# Copyright 2022, Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A tff.aggregator for discretizing input values to the integer grid."""

import collections
from typing import Optional

import numpy as np
import tensorflow as tf

from tensorflow_federated.python.aggregators import factory
from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.core.environments.tensorflow_frontend import tensorflow_computation
from tensorflow_federated.python.core.impl.federated_context import federated_computation
from tensorflow_federated.python.core.impl.federated_context import intrinsics
from tensorflow_federated.python.core.impl.types import computation_types
from tensorflow_federated.python.core.impl.types import placements
from tensorflow_federated.python.core.impl.types import type_analysis
from tensorflow_federated.python.core.impl.types import type_conversions
from tensorflow_federated.python.core.templates import aggregation_process
from tensorflow_federated.python.core.templates import measured_process

OUTPUT_TF_TYPE = tf.int32


class DeterministicDiscretizationFactory(factory.UnweightedAggregationFactory):
  """Aggregation factory for discretization of floating point tensors.

  The created `tff.templates.AggregationProcess` takes an input tensor structure
  and, for each tensor, scales and rounds the values to integer values.

  Scaling is parametrized by `step_size` which defines discretized levels
  as `x = round(x / step_size)`. A larger `step_size` produces discretized
  tensors with lower resolution. Higher resolution is achieved with a smaller
  `step_size`.

  This aggregation factory rounds the scaled tensors deterministically to the
  nearest integer. It is not compatible with DP as it does not control L2 norm
  inflation.

  The structure of the input is kept, and all values of the component tensors
  are scaled, rounded, and cast to 32-bit integers.

  This aggregator only accepts `value_type` of either `tff.TensorType` or
  `tff.StructWithPythonType` and expects the dtype of component tensors to be
  all real floats, and it will otherwise raise an error.

  The process returns `state` from the inner aggregation process, the descaled
  client values in `result` and a dictionary in `measurements` mapping the
  inner aggregation process measurements to key `deterministic_discretization`
  and optionally the distortion across clients to key `distortion` using the
  `distortion_aggregation_factory`, if provided.
  """

  def __init__(
      self,
      inner_agg_factory: factory.UnweightedAggregationFactory,
      step_size: float,
      distortion_aggregation_factory: Optional[
          factory.UnweightedAggregationFactory
      ] = None,
  ):
    """Constructor for DeterministicDiscretizationFactory.

    Args:
      inner_agg_factory: The inner `UnweightedAggregationFactory` to aggregate
        the values after the input values are discretized to the integer grid.
      step_size: A float that determines the step size between adjacent
        quantization levels to be used as the initial scale factor.
      distortion_aggregation_factory: A
        `tff.aggregators.UnweightedAggregationFactory` that is used to report
        the distortion across clients as a measurement. If None, does not report
        distortion measurement.
    """
    self._step_size = step_size
    self._inner_agg_factory = inner_agg_factory

    if distortion_aggregation_factory is not None:
      py_typecheck.check_type(
          distortion_aggregation_factory, factory.UnweightedAggregationFactory
      )
    self._distortion_aggregation_factory = distortion_aggregation_factory

  def create(
      self, value_type: factory.ValueType
  ) -> aggregation_process.AggregationProcess:
    # Validate input args and value_type and parse out the TF dtypes.
    if isinstance(value_type, computation_types.TensorType):
      tf_dtype = value_type.dtype
    elif isinstance(
        value_type, computation_types.StructWithPythonType
    ) and type_analysis.is_structure_of_tensors(value_type):
      tf_dtype = type_conversions.structure_from_tensor_type_tree(
          lambda x: x.dtype, value_type
      )
    else:
      raise TypeError(
          'Expected `value_type` to be `TensorType` or '
          '`StructWithPythonType` containing only `TensorType`. '
          f'Found type: {repr(value_type)}'
      )

    # Check that all values are floats.
    if not type_analysis.is_structure_of_floats(value_type):
      raise TypeError(
          'Component dtypes of `value_type` must all be floats. '
          f'Found {repr(value_type)}.'
      )

    if self._distortion_aggregation_factory is not None:
      distortion_aggregation_process = (
          self._distortion_aggregation_factory.create(
              computation_types.to_type(np.float32)
          )  # pytype: disable=wrong-arg-types
      )

    @tensorflow_computation.tf_computation(value_type, np.float32)
    def discretize_fn(value, step_size):
      return _discretize_struct(value, step_size)

    @tensorflow_computation.tf_computation(
        discretize_fn.type_signature.result, np.float32
    )
    def undiscretize_fn(value, step_size):
      return _undiscretize_struct(value, step_size, tf_dtype)

    @tensorflow_computation.tf_computation(value_type, np.float32)
    def distortion_measurement_fn(value, step_size):
      reconstructed_value = undiscretize_fn(
          discretize_fn(value, step_size), step_size
      )
      err = tf.nest.map_structure(tf.subtract, reconstructed_value, value)
      squared_err = tf.nest.map_structure(tf.square, err)
      flat_squared_errs = [
          tf.cast(tf.reshape(t, [-1]), tf.float32)
          for t in tf.nest.flatten(squared_err)
      ]
      all_squared_errs = tf.concat(flat_squared_errs, axis=0)
      mean_squared_err = tf.reduce_mean(all_squared_errs)
      return mean_squared_err

    inner_agg_process = self._inner_agg_factory.create(
        discretize_fn.type_signature.result
    )

    @federated_computation.federated_computation()
    def init_fn():
      state = collections.OrderedDict(
          step_size=intrinsics.federated_value(
              self._step_size, placements.SERVER
          ),
          inner_agg_process=inner_agg_process.initialize(),
      )
      return intrinsics.federated_zip(state)

    @federated_computation.federated_computation(
        init_fn.type_signature.result, computation_types.at_clients(value_type)
    )
    def next_fn(state, value):
      server_step_size = state['step_size']
      client_step_size = intrinsics.federated_broadcast(server_step_size)

      discretized_value = intrinsics.federated_map(
          discretize_fn, (value, client_step_size)
      )

      inner_state = state['inner_agg_process']
      inner_agg_output = inner_agg_process.next(inner_state, discretized_value)

      undiscretized_agg_value = intrinsics.federated_map(
          undiscretize_fn, (inner_agg_output.result, server_step_size)
      )

      new_state = collections.OrderedDict(
          step_size=server_step_size, inner_agg_process=inner_agg_output.state
      )
      measurements = collections.OrderedDict(
          deterministic_discretization=inner_agg_output.measurements
      )

      if self._distortion_aggregation_factory is not None:
        distortions = intrinsics.federated_map(
            distortion_measurement_fn, (value, client_step_size)
        )
        aggregate_distortion = distortion_aggregation_process.next(
            distortion_aggregation_process.initialize(), distortions
        ).result
        measurements['distortion'] = aggregate_distortion

      return measured_process.MeasuredProcessOutput(
          state=intrinsics.federated_zip(new_state),
          result=undiscretized_agg_value,
          measurements=intrinsics.federated_zip(measurements),
      )

    return aggregation_process.AggregationProcess(init_fn, next_fn)


def _discretize_struct(struct, step_size):
  """Scales and rounds each tensor of the structure to the integer grid."""

  def discretize_tensor(x):
    scaled_x = tf.divide(tf.cast(x, tf.float32), step_size)
    discretized_x = tf.round(scaled_x)
    return tf.cast(discretized_x, OUTPUT_TF_TYPE)

  return tf.nest.map_structure(discretize_tensor, struct)


def _undiscretize_struct(struct, step_size, tf_dtype_struct):
  """Unscales the discretized structure and casts back to original dtypes."""

  def undiscretize_tensor(x, original_dtype):
    unscaled_x = tf.cast(x, tf.float32) * step_size
    return tf.cast(unscaled_x, original_dtype)

  return tf.nest.map_structure(undiscretize_tensor, struct, tf_dtype_struct)
