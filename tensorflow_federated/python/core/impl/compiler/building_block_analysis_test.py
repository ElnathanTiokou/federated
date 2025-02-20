# Copyright 2019, The TensorFlow Federated Authors.
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

from absl.testing import absltest
import numpy as np
import tensorflow as tf

from tensorflow_federated.proto.v0 import computation_pb2 as pb
from tensorflow_federated.python.core.impl.compiler import building_block_analysis
from tensorflow_federated.python.core.impl.compiler import building_blocks
from tensorflow_federated.python.core.impl.types import computation_types
from tensorflow_federated.python.core.impl.types import type_serialization
from tensorflow_federated.python.core.impl.utils import tensorflow_utils
from tensorflow_federated.python.tensorflow_libs import serialization_utils


class CountTensorFlowOpsTest(absltest.TestCase):

  def test_raises_on_none(self):
    with self.assertRaises(TypeError):
      building_block_analysis.count_tensorflow_ops_in(None)

  def test_raises_on_reference(self):
    ref = building_blocks.Reference('x', np.int32)
    with self.assertRaises(ValueError):
      building_block_analysis.count_tensorflow_ops_in(ref)

  def test_counts_correct_number_of_ops_simple_case(self):
    with tf.Graph().as_default() as g:
      a = tf.constant(0)
      b = tf.constant(1)
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    tf_ops_in_graph = building_block_analysis.count_tensorflow_ops_in(
        building_block
    )
    # Expect 4 ops: two constants, one addition, and an identity on the result.
    self.assertEqual(tf_ops_in_graph, 4)

  def test_counts_correct_number_of_ops_with_function(self):
    @tf.function
    def add_one(x):
      return x + 1

    with tf.Graph().as_default() as graph:
      parameter_value, parameter_binding = (
          tensorflow_utils.stamp_parameter_in_graph('x', np.int32, graph)
      )
      result = add_one(add_one(parameter_value))

    result_type, result_binding = tensorflow_utils.capture_result_from_graph(
        result, graph
    )
    type_signature = computation_types.FunctionType(np.int32, result_type)
    tensorflow = pb.TensorFlow(
        graph_def=serialization_utils.pack_graph_def(graph.as_graph_def()),
        parameter=parameter_binding,
        result=result_binding,
    )
    proto = pb.Computation(
        type=type_serialization.serialize_type(type_signature),
        tensorflow=tensorflow,
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)

    tf_ops_in_graph = building_block_analysis.count_tensorflow_ops_in(
        building_block
    )

    # Expect 7 ops:
    #    Inside the tf.function:
    #      - one constant
    #      - one addition
    #      - one identity on the result
    #    Inside the tff_computation:
    #      - one placeholders (one for the argument)
    #      - two partition calls
    #      - one identity on the tff_computation result
    self.assertEqual(tf_ops_in_graph, 7)


class CountTensorFlowVariablesTest(absltest.TestCase):

  def test_raises_on_none(self):
    with self.assertRaises(TypeError):
      building_block_analysis.count_tensorflow_variables_in(None)

  def test_counts_no_variables(self):
    with tf.Graph().as_default() as g:
      a = tf.constant(0)
      b = tf.constant(1)
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    tf_vars_in_graph = building_block_analysis.count_tensorflow_variables_in(
        building_block
    )
    self.assertEqual(tf_vars_in_graph, 0)

  def test_avoids_misdirection_with_name(self):
    with tf.Graph().as_default() as g:
      a = tf.constant(0, name='variable1')
      b = tf.constant(1, name='variable2')
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    tf_vars_in_graph = building_block_analysis.count_tensorflow_variables_in(
        building_block
    )
    self.assertEqual(tf_vars_in_graph, 0)

  def test_counts_two_variables_correctly(self):
    with tf.Graph().as_default() as g:
      a = tf.Variable(0, name='variable1')
      b = tf.Variable(1, name='variable2')
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    tf_vars_in_graph = building_block_analysis.count_tensorflow_variables_in(
        building_block
    )
    self.assertEqual(tf_vars_in_graph, 2)

  def test_counts_correct_variables_with_function(self):
    @tf.function
    def add_one(x):
      with tf.init_scope():
        y = tf.Variable(1)
      return x + y

    with tf.Graph().as_default() as graph:
      parameter_value, parameter_binding = (
          tensorflow_utils.stamp_parameter_in_graph('x', np.int32, graph)
      )
      result = add_one(add_one(parameter_value))

    result_type, result_binding = tensorflow_utils.capture_result_from_graph(
        result, graph
    )
    type_signature = computation_types.FunctionType(np.int32, result_type)
    tensorflow = pb.TensorFlow(
        graph_def=serialization_utils.pack_graph_def(graph.as_graph_def()),
        parameter=parameter_binding,
        result=result_binding,
    )
    proto = pb.Computation(
        type=type_serialization.serialize_type(type_signature),
        tensorflow=tensorflow,
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)

    tf_vars_in_graph = building_block_analysis.count_tensorflow_variables_in(
        building_block
    )

    self.assertEqual(tf_vars_in_graph, 1)


class GetDevicePlacementInTest(absltest.TestCase):

  def test_raises_with_reference(self):
    ref = building_blocks.Reference('x', np.int32)
    with self.assertRaisesRegex(ValueError, 'tensorflow'):
      building_block_analysis.get_device_placement_in(ref)

  def test_gets_none_placement(self):
    with tf.Graph().as_default() as g:
      a = tf.Variable(0, name='variable1')
      b = tf.Variable(1, name='variable2')
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    device_placements = building_block_analysis.get_device_placement_in(
        building_block
    )
    all_device_placements = list(device_placements.keys())
    self.assertLen(all_device_placements, 1)
    self.assertEqual(all_device_placements[0], '')
    self.assertGreater(device_placements[''], 0)

  def test_gets_all_explicit_placement(self):
    with tf.Graph().as_default() as g:
      with tf.device('/cpu:0'):
        a = tf.constant(0)
        b = tf.constant(1)
        c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    device_placements = building_block_analysis.get_device_placement_in(
        building_block
    )
    all_device_placements = list(sorted(device_placements.keys()))
    # Expect two placements, the explicit 'cpu' from above, and the empty
    # placement of the `tf.identity` op add to the captured result.
    self.assertLen(all_device_placements, 2)
    self.assertEqual('', sorted(all_device_placements)[0])
    self.assertIn('CPU', sorted(all_device_placements)[1])
    self.assertGreater(device_placements[all_device_placements[1]], 0)

  def test_gets_some_explicit_some_none_placement(self):
    with tf.Graph().as_default() as g:
      with tf.device('/cpu:0'):
        a = tf.constant(0)
      b = tf.constant(1)
      c = a + b

    _, result_binding = tensorflow_utils.capture_result_from_graph(c, g)

    packed_graph_def = serialization_utils.pack_graph_def(g.as_graph_def())
    function_type = computation_types.FunctionType(None, np.int32)
    proto = pb.Computation(
        type=type_serialization.serialize_type(function_type),
        tensorflow=pb.TensorFlow(
            graph_def=packed_graph_def, parameter=None, result=result_binding
        ),
    )
    building_block = building_blocks.ComputationBuildingBlock.from_proto(proto)
    device_placements = building_block_analysis.get_device_placement_in(
        building_block
    )
    all_device_placements = list(device_placements.keys())
    self.assertLen(all_device_placements, 2)
    if all_device_placements[0]:
      self.assertIn('CPU', all_device_placements[0])
      self.assertEqual('', all_device_placements[1])
    else:
      self.assertIn('CPU', all_device_placements[1])
      self.assertEqual('', all_device_placements[0])
    self.assertGreater(device_placements[all_device_placements[0]], 0)
    self.assertGreater(device_placements[all_device_placements[1]], 0)


if __name__ == '__main__':
  absltest.main()
