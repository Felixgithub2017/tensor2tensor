# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Mixture-of-experts code.

Interfaces and algorithms are under development and subject to rapid change
without notice.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


from tensor2tensor.mesh_tensorflow import mesh_tensorflow as mtf
from tensor2tensor.mesh_tensorflow import mtf_layers
import tensorflow as tf




def transformer_moe_layer_v1(inputs, output_dim, hparams, train):
  """Local mixture of experts that works well on TPU.

  Adapted from the paper https://arxiv.org/abs/1701.06538

  Note: until the algorithm and inferface solidify, we pass in a hyperparameters
  dictionary in order not to complicate the interface in mtf_transformer.py .
  Once this code moves out of "research", we should pass the hyperparameters
  separately.

  Hyperparameters used:
    hparams.moe_num_experts: number of experts
    hparams.moe_hidden_size: size of hidden layer in each expert
    hparams.moe_group_size: size of each "group" for gating purposes
    hparams.moe_capacity_factor_train: a float
    hparams.moe_capacity_factor_eval: a float
    hparams.moe_gating: a string
    + all hyperparmeters used by _top_2_gating()

  The number of parameters in the gating network is:
    (input_dim.size * hparams.num_experts) +

  The number of parameters in the experts themselves is:
    (hparams.num_experts
     * (input_dim.size + output_dim.size)
     * hparams.moe_hidden_size)

  The input is n-dimensional: [<batch_and_length_dims>, input_dim], consisting
  of the representations of all positions in a batch of sequences.

  Each position of each sequence is sent to 0-2 experts.  The expert
  choices and the combination weights are determined by a learned gating
  function.

  This function returns a small auxiliary loss that should be added to the
  training loss of the model.  This loss helps to balance expert usage.
  Without the loss, it is very likely that a few experts will be trained and
  the rest will starve.

  Several hacks are necessary to get around current TPU limitations:

  - To ensure static shapes, we enforce (by truncation/padding)
    that each sequence send the same number of elements to each expert.

    It would make more sense to enforce this equality over the entire batch,
    but due to our hacked-up gather-by-matmul implementation, we need to divide
    the batch into "groups".  For each group, the same number of elements
    are sent to each expert.

  TODO(noam): Factor this code better.  We want to be able to substitute
  different code for the experts themselves.

  Args:
    inputs: a mtf.Tensor with shape [<batch_dims...>, length_dim, input_dim]
    output_dim: a mtf.Dimension (for Transformer, this is input_dim)
    hparams: model hyperparameters
    train: a boolean

  Returns:
    outputs: a Tensor with shape [<batch_dims...>, length_dim, output_dim]
    loss: a mtf scalar

  Raises:
    ValueError: on unrecognized hparams.moe_gating
  """
  orig_inputs = inputs
  input_dim = inputs.shape.dims[-1]
  hidden_dim = mtf.Dimension("expert_hidden", hparams.moe_hidden_size)
  experts_dim = mtf.Dimension("experts", hparams.moe_num_experts)
  group_size_dim = mtf.Dimension("group", hparams.moe_group_size)
  batch_dim = mtf.Dimension(
      orig_inputs.shape[0].name,
      orig_inputs.shape.size // (group_size_dim.size * input_dim.size))
  inputs = mtf.reshape(inputs, [batch_dim, group_size_dim, input_dim])

  # Each sequence sends expert_capacity positions to each expert.
  capacity_factor = (
      hparams.moe_capacity_factor_train if train else
      hparams.moe_capacity_factor_eval)
  expert_capacity = min(
      group_size_dim.size,
      int((group_size_dim.size * capacity_factor) / experts_dim.size))
  expert_capacity_dim = mtf.Dimension("expert_capacity", expert_capacity)

  experts_dim_unsplit = mtf.Dimension("expert_unsplit", experts_dim.size)
  batch_dim_unsplit = mtf.Dimension("batch_unsplit", batch_dim.size)

  if hparams.moe_gating == "top_2":
    dispatch_tensor, combine_tensor, loss = _top_2_gating(
        inputs=inputs,
        outer_expert_dims=None,
        experts_dim=experts_dim_unsplit,
        expert_capacity_dim=expert_capacity_dim,
        max_experts=None,
        hparams=hparams,
        train=train)
  else:
    raise ValueError("unknown hparams.moe_gating=%s" % hparams.moe_gating)

  # put num_experts dimension first to make split easier in alltoall
  expert_inputs = mtf.einsum([inputs, dispatch_tensor], mtf.Shape(
      [experts_dim_unsplit, batch_dim, expert_capacity_dim, input_dim]))

  expert_inputs = mtf.reshape(expert_inputs, mtf.Shape(
      [experts_dim, batch_dim_unsplit, expert_capacity_dim, input_dim]))

  # Now feed the expert inputs through the experts.
  h = mtf_layers.dense(
      expert_inputs, hidden_dim, expert_dims=[experts_dim],
      activation=mtf.relu, use_bias=False, name="x0")
  expert_output = mtf_layers.dense(
      h, output_dim, expert_dims=[experts_dim], use_bias=False, name="x1")

  expert_output = mtf.reshape(expert_output, mtf.Shape(
      [experts_dim_unsplit, batch_dim, expert_capacity_dim, input_dim]))

  output = mtf.einsum([expert_output, combine_tensor], mtf.Shape(
      [batch_dim, group_size_dim, output_dim]))

  output = mtf.reshape(output, orig_inputs.shape.dims[:-1] + [output_dim])

  return output, loss * hparams.moe_loss_coef


def transformer_moe_layer_v2(inputs, output_dim, hparams, train):
  """2-level mixture of experts.

  Adapted from the paper https://arxiv.org/abs/1701.06538

  Note: until the algorithm and inferface solidify, we pass in a hyperparameters
  dictionary in order not to complicate the interface in mtf_transformer.py .
  Once this code moves out of "research", we should pass the hyperparameters
  separately.

  Hyperparameters used:
    hparams.moe_num_experts: number of experts
    hparams.moe_hidden_size: size of hidden layer in each expert
    hparams.moe_group_size: size of each "group" for gating purposes
    hparams.moe_capacity_factor_train: a float
    hparams.moe_capacity_factor_eval: a float
    hparams.moe_gating: a string
    + all hyperparmeters used by _top_2_gating()

  One set of params for experts in first level and different of hparams
  per expert in the second level.
  The number of parameters in the gating network is:
    (input_dim.size * (hparams.num_experts) +
      (moe_hidden_size * hparams.num_experts) * hparams.num_experts


  The number of parameters in the experts themselves is:
    (hparams.num_experts
     * (input_dim.size + output_dim.size)
     * hparams.moe_hidden_size)

  The input is n-dimensional: [<batch_and_length_dims>, input_dim], consisting
  of the representations of all positions in a batch of sequences.

  Each position of each sequence is sent to 0-2 experts.  The expert
  choices and the combination weights are determined by a learned gating
  function.

  This function returns a small auxiliary loss that should be added to the
  training loss of the model.  This loss helps to balance expert usage.
  Without the loss, it is very likely that a few experts will be trained and
  the rest will starve.

  Several hacks are necessary to get around current TPU limitations:

  - To ensure static shapes, we enforce (by truncation/padding)
    that each sequence send the same number of elements to each expert.

    It would make more sense to enforce this equality over the entire batch,
    but due to our hacked-up gather-by-matmul implementation, we need to divide
    the batch into "groups".  For each group, the same number of elements
    are sent to each expert.

  TODO(noam): Factor this code better.  We want to be able to substitute
  different code for the experts themselves.

  Dimensions cheat sheet:
  a, b: batch size
  l: original sequence length
  m: input depth
  n: output depth
  g, h: number of groups
  s, t: group size
  x, y: number of experts
  c, d: expert capacity

  input: [a0, b1, l, m]
  input: [a0, g1, s, m]
  dispatch_tensor_x: [a0, g1, s, x, c]
  expert_input: [a0, g1, x, c, m]
  alltoall: [a0, g, x1, c, m]
  alltoall: [a0, g, x1, c, m]
  transpose: [x1, a0, g, c, m]
  reshape: [x1, h0, s, m]
  assignment2: [x1, h0, t, y, d]
  expert_input2: [x1, h0, y, d, m]
  alltoall: [x1, h, y0, d, m]
  ...
  reverse of that

  gating params 0: [m, x]
  gating params 1: [x1, m, y]

  expert params:
     [x1, y0, m, hidden]
     [x1, y0, hidden, n]

  Args:
    inputs: a mtf.Tensor with shape [a, b, l, m]
    output_dim: a mtf.Dimension (for Transformer, this is input_dim)
    hparams: model hyperparameters
    train: a boolean

  Returns:
    outputs: a Tensor with shape [a, b, l, n]
    loss: a mtf scalar

  Raises:
    ValueError: on unrecognized hparams.moe_gating
  """
  assert len(hparams.moe_num_experts) == 2
  a0, b1, l, m = inputs.shape.dims
  # a = mtf.Dimension("a_unsplit", a0.size)
  # b = mtf.Dimension("b_unsplit", b1.size)
  hidden_dim = mtf.Dimension("expert_hidden", hparams.moe_hidden_size)
  x1 = mtf.Dimension("expert_x", hparams.moe_num_experts[0])
  y0 = mtf.Dimension("expert_y", hparams.moe_num_experts[1])
  x = mtf.Dimension("expert_x_unsplit", hparams.moe_num_experts[0])
  y = mtf.Dimension("expert_y_unsplit", hparams.moe_num_experts[1])
  s = mtf.Dimension("group_size_x", hparams.moe_group_size[0])
  t = mtf.Dimension("group_size_y", hparams.moe_group_size[1])
  g1 = mtf.Dimension(b1.name, b1.size * l.size // s.size)
  g = mtf.Dimension(b1.name + "_unsplit", b1.size * l.size // s.size)
  n = output_dim

  # First level of expert routing
  inputs = mtf.reshape(inputs, [a0, g1, s, m])

  # Each sequence sends (at most?) expert_capacity positions to each expert.
  # Static expert_capacity dimension is needed for expert batch sizes
  capacity_factor = (
      hparams.moe_capacity_factor_train if train else
      hparams.moe_capacity_factor_eval)
  expert_capacity = min(
      s.size,
      int((s.size * capacity_factor) / x.size))
  c = mtf.Dimension("expert_capacity_x", expert_capacity)

  # Get the assignments for the first level.
  # dispatch_tensor_x has shape [a0, g1, s, x, c]
  if hparams.moe_gating == "top_2":
    dispatch_tensor_x, combine_tensor_x, loss_outer = _top_2_gating(
        inputs=inputs,
        outer_expert_dims=None,
        experts_dim=x,
        expert_capacity_dim=c,
        max_experts=None,
        hparams=hparams,
        train=train)
  else:
    raise ValueError("unknown hparams.moe_gating=%s" % hparams.moe_gating)

  # Now create expert_inputs based on the assignments.
  # put num_experts dimension first to make split easier in alltoall
  expert_inputs_x = mtf.einsum([inputs, dispatch_tensor_x], mtf.Shape(
      [x, a0, g1, c, m]))

  # First level, all to all. Here we change the split dimension from g1 to x1.
  expert_inputs_x = mtf.reshape(expert_inputs_x, mtf.Shape(
      [x1, a0, g, c, m]))

  # Second level of expert routing

  numerator = a0.size * g.size * c.size
  if numerator % t.size != 0:
    raise ValueError("cannont divide evenly %s / %s" % (numerator, t.size))
  h0 = mtf.Dimension(a0.name, numerator // t.size)
  h = mtf.Dimension(a0.name + "_unsplit", h0.size)
  inputs_y = mtf.reshape(expert_inputs_x, [x1, h0, t, m])

  expert_capacity = min(
      t.size,
      int((t.size * capacity_factor) / y.size))
  d = mtf.Dimension("expert_capacity_y", expert_capacity)

  # Get the assignments for the second level.
  # dispatch_tensor_x has shape [x1, h0, t, y, d]
  if hparams.moe_gating == "top_2":
    dispatch_tensor_y, combine_tensor_y, loss_inner = _top_2_gating(
        inputs=inputs_y,
        outer_expert_dims=[x1],
        experts_dim=y,
        expert_capacity_dim=d,
        max_experts=None,
        hparams=hparams,
        train=train)
  else:
    raise ValueError("unknown hparams.moe_gating=%s" % hparams.moe_gating)

  # Now create expert_inputs based on the assignments.
  # put num_experts dimension first to make split easier in alltoall
  expert_inputs_y = mtf.einsum([inputs_y, dispatch_tensor_y], mtf.Shape(
      [y, x1, h0, d, m]))

  # Second level, all to all. Here we change the split dimension from h0 to y0.
  expert_inputs_y = mtf.reshape(expert_inputs_y, mtf.Shape(
      [y0, x1, h, d, m]))

  # Now feed the expert inputs through the experts.
  hidden_output = mtf_layers.dense(
      expert_inputs_y, hidden_dim, expert_dims=[y0, x1],
      activation=mtf.relu, use_bias=False, name="expert0")
  expert_output = mtf_layers.dense(
      hidden_output, output_dim, expert_dims=[y0, x1],
      use_bias=False, name="expert1")

  # NOW COMBINE EXPERT OUTPUTS (reversing everything we have done)
  # expert_output has shape [y0, x1, h, d, n]

  # alltoall
  expert_output = mtf.reshape(expert_output, mtf.Shape(
      [y, x1, h0, d, n]))

  # combine results from inner level
  output_y = mtf.einsum([expert_output, combine_tensor_y], mtf.Shape(
      [x1, h0, t, n]))

  # simple reshape
  output = mtf.reshape(output_y, [x1, a0, g, c, n])

  # alltoall
  expert_output_x = mtf.reshape(output, mtf.Shape([x, a0, g1, c, n]))

  # combine results from outer level
  output_x = mtf.einsum([expert_output_x, combine_tensor_x], mtf.Shape(
      [a0, g1, s, m]))

  # simple reshape
  output = mtf.reshape(output_x, [a0, b1, l, n])
  return output, (loss_outer + loss_inner) * hparams.moe_loss_coef


def _top_2_gating(
    inputs, outer_expert_dims, experts_dim, expert_capacity_dim, max_experts,
    hparams, train):
  """Compute gating for mixture-of-experts in TensorFlow.

  Note: until the algorithm and inferface solidify, we pass in a hyperparameters
  dictionary in order not to complicate the interface in mtf_transformer.py .
  Once this code moves out of "research", we should pass the hyperparameters
  separately.

  Hyperparameters used:
    hparams.moe_use_second_place_loss: a boolean
    hparams.moe_second_policy_train: a string
    hparams.moe_second_policy_eval: a string
    hparams.moe_second_threshold: a float

  max_experts is an float tensor with shape [<batch_dims>, group_size_dim]
  indicating at most how many experts to use per example.  This can be
  used to prevent padding from going to experts.

  The returned forward assignment is a tensor used to map (via einsum) from the
  inputs to the expert_inputs.  Likewise, the returned combine_tensor is
  used to map (via einsum) from the expert outputs to the outputs.  Both the
  forward and backward assignments are mostly zeros.  The shapes of all of these
  are as follows.

  inputs: [<batch_dims>, group_size_dim, input_dim]
  dispatch_tensor:
    [<batch_dims>, group_size_dim, experts_dim, expert_capacity_dim]
  expert_inputs:
    [<batch_dims>, experts_dim, expert_capacity_dim, input_dim]

  expert_outputs: [<batch_dims>, experts_dim, expert_capacity_dim, output_dim]
  combine_tensor:
    [<batch_dims>, group_size_dim, experts_dim, expert_capacity_dim]
  outputs: [<batch_dims>, group_size_dim, output_dim]

  Args:
    inputs: a mtf.Tensor with shape [<batch_dims>, group_size_dim, input_dim]
    outer_expert_dims: an optional list of dimensions.  This is for the case
      where we are at an inner level of a hierarchical MoE.
    experts_dim: a Dimension (the number of experts)
    expert_capacity_dim: a Dimension (number of examples per group per expert)
    max_experts: optional mtf.Tensor with shape [<batch_dims>, group_size_dim]
    hparams: model hyperparameters.
    train: a boolean

  Returns:
    dispatch_tensor: a Tensor with shape
      [<batch_dims>, group_size_dim, experts_dim, expert_capacity_dim]
    combine_tensor: a Tensor with shape
      [<batch_dims>, group_size_dim, experts_dim, expert_capacity_dim]
    loss: a mtf scalar

  Raises:
    ValueError: on illegal hyperparameters
  """
  group_size_dim, unused_input_dim = inputs.shape.dims[-2:]

  raw_gates = mtf.softmax(mtf_layers.dense(
      inputs, experts_dim, use_bias=False,
      expert_dims=outer_expert_dims), experts_dim)

  expert_capacity_f = float(expert_capacity_dim.size)

  # FIND TOP 2 EXPERTS PER POSITON
  # Find the top expert for each position. shape=[batch, group]
  index_1, gate_1 = mtf.top_1(raw_gates, experts_dim)
  # [batch, group, experts]
  mask_1 = mtf.one_hot(index_1, experts_dim, dtype=raw_gates.dtype)
  gates_without_top_1 = raw_gates * (1.0 - mask_1)
  # [batch, group]
  index_2, gate_2 = mtf.top_1(gates_without_top_1, experts_dim)
  # [batch, group, experts]
  mask_2 = mtf.one_hot(index_2, experts_dim, dtype=raw_gates.dtype)

  if max_experts is not None:
    geq1 = mtf.to_float(mtf.greater_equal(max_experts, 1.0))
    geq2 = mtf.to_float(mtf.greater_equal(max_experts, 2.0))
    mask_1 *= geq1
    mask_2 *= geq2
    raw_gates *= geq1
    gates_without_top_1 *= geq2

  # BALANCING LOSSES
  # shape = [batch, experts]
  # We want to equalize the fraction of the batch assigned to each expert
  density_1 = mtf.reduce_mean(mask_1, reduced_dim=group_size_dim)
  # Something continuous that is correlated with what we want to equalize.
  density_1_proxy = mtf.reduce_mean(raw_gates, reduced_dim=group_size_dim)
  density_1 = mtf.Print(
      density_1, [mtf.reduce_mean(density_1, output_shape=[experts_dim])],
      "density_1", summarize=1000)
  loss = (mtf.reduce_mean(density_1_proxy * density_1)
          * float(experts_dim.size * experts_dim.size))

  if hparams.moe_use_second_place_loss:
    # Also add a loss to encourage all experts to be used equally also as the
    # second-place expert.  Experimentally, this seems to be a wash.
    # We want to equalize the fraction of the batch assigned to each expert:
    density_2 = mtf.reduce_mean(mask_2, reduced_dim=group_size_dim)
    # As a proxy for density_2, we renormalize the raw gates after the top one
    # has been removed.
    normalized = gates_without_top_1 / (
        mtf.reduce_sum(gates_without_top_1, reduced_dim=experts_dim) + 1e-9)
    density_2_proxy = mtf.reduce_mean(normalized, reduced_dim=group_size_dim)
    loss_2 = (mtf.reduce_mean(density_2_proxy * density_2)
              * float(experts_dim.size * experts_dim.size))
    loss += loss_2 * 0.5

  # Depending on the policy in the hparams, we may drop out some of the
  # second-place experts.
  policy = (
      hparams.moe_second_policy_train if train else
      hparams.moe_second_policy_eval)
  threshold = (
      hparams.moe_second_threshold_train if train else
      hparams.moe_second_threshold_eval)
  if policy == "all":
    # Use second-place experts for all examples.
    pass
  elif policy == "none":
    # Never use second-place experts for all examples.
    mask_2 = mtf.zeros_like(mask_2)
  elif policy == "threshold":
    # Use second-place experts if gate_2 > threshold.
    mask_2 *= mtf.to_float(mtf.greater(gate_2, threshold))
  elif policy == "random":
    # Use second-place experts with probablity min(1.0, gate_2 / threshold).
    mask_2 *= mtf.to_float(
        mtf.less(mtf.random_uniform(gate_2.mesh, gate_2.shape),
                 gate_2 / max(threshold, 1e-9)))
  else:
    raise ValueError("Unknown policy %s" % policy)
  mask_2 = mtf.Print(
      mask_2, [mtf.reduce_mean(mask_2, output_shape=[experts_dim])],
      "density_2", summarize=1000)

  # COMPUTE ASSIGNMENT TO EXPERTS
  # [batch, group, experts]
  # This is the position within the expert's mini-batch for this sequence
  position_in_expert_1 = mtf.cumsum(
      mask_1, group_size_dim, exclusive=True) * mask_1
  # Remove the elements that don't fit. [batch, group, experts]
  mask_1 *= mtf.to_float(mtf.less(position_in_expert_1, expert_capacity_f))
  # [batch, experts]
  # How many examples in this sequence go to this expert
  mask_1_count = mtf.reduce_sum(mask_1, reduced_dim=group_size_dim)
  # [batch, group] - mostly ones, but zeros where something didn't fit
  mask_1_flat = mtf.reduce_sum(mask_1, reduced_dim=experts_dim)
  # [batch, group]
  position_in_expert_1 = mtf.reduce_sum(
      position_in_expert_1, reduced_dim=experts_dim)
  # Weight assigned to first expert.  [batch, group]
  gate_1 *= mask_1_flat

  # [batch, group, experts]
  position_in_expert_2 = (
      mtf.cumsum(mask_2, group_size_dim, exclusive=True) + mask_1_count)
  position_in_expert_2 *= mask_2
  mask_2 *= mtf.to_float(mtf.less(position_in_expert_2, expert_capacity_f))
  # mask_2_count = mtf.reduce_sum(mask_2, reduced_dim=experts_dim)
  mask_2_flat = mtf.reduce_sum(mask_2, reduced_dim=experts_dim)
  gate_2 *= mask_2_flat
  position_in_expert_2 = mtf.reduce_sum(
      position_in_expert_2, reduced_dim=experts_dim)

  # renormalize the two gate values to add up to 1
  denom = gate_1 + gate_2 + 1e-9
  gate_1 /= denom
  gate_2 /= denom

  # [batch, group, experts, expert_capacity]
  combine_tensor = (
      gate_1 * mask_1_flat
      * mtf.one_hot(index_1, experts_dim)
      * mtf.one_hot(mtf.to_int32(position_in_expert_1), expert_capacity_dim) +
      gate_2 * mask_2_flat
      * mtf.one_hot(index_2, experts_dim)
      * mtf.one_hot(mtf.to_int32(position_in_expert_2), expert_capacity_dim))

  dispatch_tensor = mtf.cast(
      mtf.cast(combine_tensor, tf.bool), combine_tensor.dtype)

  return dispatch_tensor, combine_tensor, loss


def set_default_moe_hparams(hparams):
  """Add necessary hyperparameters for mixture-of-experts."""
  hparams.feedforward_layer = "moe"
  hparams.moe_num_experts = 16
  hparams.moe_loss_coef = 1e-2
  hparams.add_hparam("moe_gating", "top_2")
  # Experts have fixed capacity per batch.  We need some extra capacity
  # in case gating is not perfectly balanced.
  # moe_capacity_factor_* should be set to a value >=1.
  hparams.add_hparam("moe_capacity_factor_train", 1.25)
  hparams.add_hparam("moe_capacity_factor_eval", 2.0)
  # Each expert has a hidden layer with this size.
  hparams.add_hparam("moe_hidden_size", 4096)
  # For gating, divide inputs into groups of this size before gating.
  # Each group sends the same number of inputs to each expert.
  # Ideally, the group size would be the whole batch, but this is expensive
  # due to our use of matrix multiplication for reordering.
  hparams.add_hparam("moe_group_size", 1024)
  # For top_2 gating, whether to impose an additional loss in order to make
  # the experts equally used as the second-place expert.
  hparams.add_hparam("moe_use_second_place_loss", 0)
  # In top_2 gating, policy for whether to use a second-place expert.
  # Legal values are:
  #    "all": always
  #    "none": never
  #    "threshold": if gate value > the given threshold
  #    "random": if gate value > threshold*random_uniform(0,1)
  hparams.add_hparam("moe_second_policy_train", "random")
  hparams.add_hparam("moe_second_policy_eval", "random")
  hparams.add_hparam("moe_second_threshold_train", 0.2)
  hparams.add_hparam("moe_second_threshold_eval", 0.2)
