"""
这个页面的代码大量参考了 allenNLP
"""

from typing import Optional, Tuple, List, Callable

import os

import h5py
import numpy
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import PackedSequence, pad_packed_sequence
from ...core.vocabulary import Vocabulary
import json

from ..utils import get_dropout_mask
import codecs

class LstmCellWithProjection(torch.nn.Module):
    """
    An LSTM with Recurrent Dropout and a projected and clipped hidden state and
    memory. Note: this implementation is slower than the native Pytorch LSTM because
    it cannot make use of CUDNN optimizations for stacked RNNs due to and
    variational dropout and the custom nature of the cell state.
    Parameters
    ----------
    input_size : ``int``, required.
        The dimension of the inputs to the LSTM.
    hidden_size : ``int``, required.
        The dimension of the outputs of the LSTM.
    cell_size : ``int``, required.
        The dimension of the memory cell used for the LSTM.
    go_forward: ``bool``, optional (default = True)
        The direction in which the LSTM is applied to the sequence.
        Forwards by default, or backwards if False.
    recurrent_dropout_probability: ``float``, optional (default = 0.0)
        The dropout probability to be used in a dropout scheme as stated in
        `A Theoretically Grounded Application of Dropout in Recurrent Neural Networks
        <https://arxiv.org/abs/1512.05287>`_ . Implementation wise, this simply
        applies a fixed dropout mask per sequence to the recurrent connection of the
        LSTM.
    state_projection_clip_value: ``float``, optional, (default = None)
        The magnitude with which to clip the hidden_state after projecting it.
    memory_cell_clip_value: ``float``, optional, (default = None)
        The magnitude with which to clip the memory cell.
    Returns
    -------
    output_accumulator : ``torch.FloatTensor``
        The outputs of the LSTM for each timestep. A tensor of shape
        (batch_size, max_timesteps, hidden_size) where for a given batch
        element, all outputs past the sequence length for that batch are
        zero tensors.
    final_state: ``Tuple[torch.FloatTensor, torch.FloatTensor]``
        The final (state, memory) states of the LSTM, with shape
        (1, batch_size, hidden_size) and  (1, batch_size, cell_size)
        respectively. The first dimension is 1 in order to match the Pytorch
        API for returning stacked LSTM states.
    """

    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 cell_size: int,
                 go_forward: bool = True,
                 recurrent_dropout_probability: float = 0.0,
                 memory_cell_clip_value: Optional[float] = None,
                 state_projection_clip_value: Optional[float] = None) -> None:
        super(LstmCellWithProjection, self).__init__()
        # Required to be wrapped with a :class:`PytorchSeq2SeqWrapper`.
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.cell_size = cell_size

        self.go_forward = go_forward
        self.state_projection_clip_value = state_projection_clip_value
        self.memory_cell_clip_value = memory_cell_clip_value
        self.recurrent_dropout_probability = recurrent_dropout_probability

        # We do the projections for all the gates all at once.
        self.input_linearity = torch.nn.Linear(input_size, 4 * cell_size, bias=False)
        self.state_linearity = torch.nn.Linear(hidden_size, 4 * cell_size, bias=True)

        # Additional projection matrix for making the hidden state smaller.
        self.state_projection = torch.nn.Linear(cell_size, hidden_size, bias=False)
        self.reset_parameters()

    def reset_parameters(self):
        # Use sensible default initializations for parameters.
        nn.init.orthogonal_(self.input_linearity.weight.data)
        nn.init.orthogonal_(self.state_linearity.weight.data)

        self.state_linearity.bias.data.fill_(0.0)
        # Initialize forget gate biases to 1.0 as per An Empirical
        # Exploration of Recurrent Network Architectures, (Jozefowicz, 2015).
        self.state_linearity.bias.data[self.cell_size:2 * self.cell_size].fill_(1.0)

    def forward(self,  # pylint: disable=arguments-differ
                inputs: torch.FloatTensor,
                batch_lengths: List[int],
                initial_state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None):
        """
        Parameters
        ----------
        inputs : ``torch.FloatTensor``, required.
            A tensor of shape (batch_size, num_timesteps, input_size)
            to apply the LSTM over.
        batch_lengths : ``List[int]``, required.
            A list of length batch_size containing the lengths of the sequences in batch.
        initial_state : ``Tuple[torch.Tensor, torch.Tensor]``, optional, (default = None)
            A tuple (state, memory) representing the initial hidden state and memory
            of the LSTM. The ``state`` has shape (1, batch_size, hidden_size) and the
            ``memory`` has shape (1, batch_size, cell_size).
        Returns
        -------
        output_accumulator : ``torch.FloatTensor``
            The outputs of the LSTM for each timestep. A tensor of shape
            (batch_size, max_timesteps, hidden_size) where for a given batch
            element, all outputs past the sequence length for that batch are
            zero tensors.
        final_state : ``Tuple[``torch.FloatTensor, torch.FloatTensor]``
            A tuple (state, memory) representing the initial hidden state and memory
            of the LSTM. The ``state`` has shape (1, batch_size, hidden_size) and the
            ``memory`` has shape (1, batch_size, cell_size).
        """
        batch_size = inputs.size()[0]
        total_timesteps = inputs.size()[1]

        # We have to use this '.data.new().fill_' pattern to create tensors with the correct
        # type - forward has no knowledge of whether these are torch.Tensors or torch.cuda.Tensors.
        output_accumulator = inputs.data.new(batch_size,
                                             total_timesteps,
                                             self.hidden_size).fill_(0)
        if initial_state is None:
            full_batch_previous_memory = inputs.data.new(batch_size,
                                                         self.cell_size).fill_(0)
            full_batch_previous_state = inputs.data.new(batch_size,
                                                        self.hidden_size).fill_(0)
        else:
            full_batch_previous_state = initial_state[0].squeeze(0)
            full_batch_previous_memory = initial_state[1].squeeze(0)

        current_length_index = batch_size - 1 if self.go_forward else 0
        if self.recurrent_dropout_probability > 0.0 and self.training:
            dropout_mask = get_dropout_mask(self.recurrent_dropout_probability,
                                            full_batch_previous_state)
        else:
            dropout_mask = None

        for timestep in range(total_timesteps):
            # The index depends on which end we start.
            index = timestep if self.go_forward else total_timesteps - timestep - 1

            # What we are doing here is finding the index into the batch dimension
            # which we need to use for this timestep, because the sequences have
            # variable length, so once the index is greater than the length of this
            # particular batch sequence, we no longer need to do the computation for
            # this sequence. The key thing to recognise here is that the batch inputs
            # must be _ordered_ by length from longest (first in batch) to shortest
            # (last) so initially, we are going forwards with every sequence and as we
            # pass the index at which the shortest elements of the batch finish,
            # we stop picking them up for the computation.
            if self.go_forward:
                while batch_lengths[current_length_index] <= index:
                    current_length_index -= 1
            # If we're going backwards, we are _picking up_ more indices.
            else:
                # First conditional: Are we already at the maximum number of elements in the batch?
                # Second conditional: Does the next shortest sequence beyond the current batch
                # index require computation use this timestep?
                while current_length_index < (len(batch_lengths) - 1) and \
                        batch_lengths[current_length_index + 1] > index:
                    current_length_index += 1

            # Actually get the slices of the batch which we
            # need for the computation at this timestep.
            # shape (batch_size, cell_size)
            previous_memory = full_batch_previous_memory[0: current_length_index + 1].clone()
            # Shape (batch_size, hidden_size)
            previous_state = full_batch_previous_state[0: current_length_index + 1].clone()
            # Shape (batch_size, input_size)
            timestep_input = inputs[0: current_length_index + 1, index]

            # Do the projections for all the gates all at once.
            # Both have shape (batch_size, 4 * cell_size)
            projected_input = self.input_linearity(timestep_input)
            projected_state = self.state_linearity(previous_state)

            # Main LSTM equations using relevant chunks of the big linear
            # projections of the hidden state and inputs.
            input_gate = torch.sigmoid(projected_input[:, (0 * self.cell_size):(1 * self.cell_size)] +
                                       projected_state[:, (0 * self.cell_size):(1 * self.cell_size)])
            forget_gate = torch.sigmoid(projected_input[:, (1 * self.cell_size):(2 * self.cell_size)] +
                                        projected_state[:, (1 * self.cell_size):(2 * self.cell_size)])
            memory_init = torch.tanh(projected_input[:, (2 * self.cell_size):(3 * self.cell_size)] +
                                     projected_state[:, (2 * self.cell_size):(3 * self.cell_size)])
            output_gate = torch.sigmoid(projected_input[:, (3 * self.cell_size):(4 * self.cell_size)] +
                                        projected_state[:, (3 * self.cell_size):(4 * self.cell_size)])
            memory = input_gate * memory_init + forget_gate * previous_memory

            # Here is the non-standard part of this LSTM cell; first, we clip the
            # memory cell, then we project the output of the timestep to a smaller size
            # and again clip it.

            if self.memory_cell_clip_value:
                # pylint: disable=invalid-unary-operand-type
                memory = torch.clamp(memory, -self.memory_cell_clip_value, self.memory_cell_clip_value)

            # shape (current_length_index, cell_size)
            pre_projection_timestep_output = output_gate * torch.tanh(memory)

            # shape (current_length_index, hidden_size)
            timestep_output = self.state_projection(pre_projection_timestep_output)
            if self.state_projection_clip_value:
                # pylint: disable=invalid-unary-operand-type
                timestep_output = torch.clamp(timestep_output,
                                              -self.state_projection_clip_value,
                                              self.state_projection_clip_value)

            # Only do dropout if the dropout prob is > 0.0 and we are in training mode.
            if dropout_mask is not None:
                timestep_output = timestep_output * dropout_mask[0: current_length_index + 1]

            # We've been doing computation with less than the full batch, so here we create a new
            # variable for the the whole batch at this timestep and insert the result for the
            # relevant elements of the batch into it.
            full_batch_previous_memory = full_batch_previous_memory.data.clone()
            full_batch_previous_state = full_batch_previous_state.data.clone()
            full_batch_previous_memory[0:current_length_index + 1] = memory
            full_batch_previous_state[0:current_length_index + 1] = timestep_output
            output_accumulator[0:current_length_index + 1, index] = timestep_output

        # Mimic the pytorch API by returning state in the following shape:
        # (num_layers * num_directions, batch_size, ...). As this
        # LSTM cell cannot be stacked, the first dimension here is just 1.
        final_state = (full_batch_previous_state.unsqueeze(0),
                       full_batch_previous_memory.unsqueeze(0))

        return output_accumulator, final_state


class LstmbiLm(nn.Module):
    def __init__(self, config):
        super(LstmbiLm, self).__init__()
        self.config = config
        self.encoder = nn.LSTM(self.config['encoder']['projection_dim'],
                               self.config['encoder']['dim'],
                               num_layers=self.config['encoder']['n_layers'],
                               bidirectional=True,
                               batch_first=True,
                               dropout=self.config['dropout'])
        self.projection = nn.Linear(self.config['encoder']['dim'], self.config['encoder']['projection_dim'], bias=True)

    def forward(self, inputs, seq_len):
        sort_lens, sort_idx = torch.sort(seq_len, dim=0, descending=True)
        inputs = inputs[sort_idx]
        inputs = nn.utils.rnn.pack_padded_sequence(inputs, sort_lens, batch_first=self.batch_first)
        output, hx = self.encoder(inputs, None)  # -> [N,L,C]
        output, _ = nn.utils.rnn.pad_packed_sequence(output, batch_first=self.batch_first)
        _, unsort_idx = torch.sort(sort_idx, dim=0, descending=False)
        output = output[unsort_idx]
        forward, backward = output.split(self.config['encoder']['dim'], 2)
        return torch.cat([self.projection(forward), self.projection(backward)], dim=2)


class ElmobiLm(torch.nn.Module):
    def __init__(self, config):
        super(ElmobiLm, self).__init__()
        self.config = config
        input_size = config['encoder']['projection_dim']
        hidden_size = config['encoder']['projection_dim']
        cell_size = config['encoder']['dim']
        num_layers = config['encoder']['n_layers']
        memory_cell_clip_value = config['encoder']['cell_clip']
        state_projection_clip_value = config['encoder']['proj_clip']
        recurrent_dropout_probability = config['dropout']

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.cell_size = cell_size

        forward_layers = []
        backward_layers = []

        lstm_input_size = input_size
        go_forward = True
        for layer_index in range(num_layers):
            forward_layer = LstmCellWithProjection(lstm_input_size,
                                                   hidden_size,
                                                   cell_size,
                                                   go_forward,
                                                   recurrent_dropout_probability,
                                                   memory_cell_clip_value,
                                                   state_projection_clip_value)
            backward_layer = LstmCellWithProjection(lstm_input_size,
                                                    hidden_size,
                                                    cell_size,
                                                    not go_forward,
                                                    recurrent_dropout_probability,
                                                    memory_cell_clip_value,
                                                    state_projection_clip_value)
            lstm_input_size = hidden_size

            self.add_module('forward_layer_{}'.format(layer_index), forward_layer)
            self.add_module('backward_layer_{}'.format(layer_index), backward_layer)
            forward_layers.append(forward_layer)
            backward_layers.append(backward_layer)
        self.forward_layers = forward_layers
        self.backward_layers = backward_layers

    def forward(self, inputs, seq_len):
        """

        :param inputs: batch_size x max_len x embed_size
        :param seq_len: batch_size
        :return: torch.FloatTensor. num_layers x batch_size x max_len x hidden_size
        """
        max_len = inputs.size(1)
        sort_lens, sort_idx = torch.sort(seq_len, dim=0, descending=True)
        inputs = inputs[sort_idx]
        inputs = nn.utils.rnn.pack_padded_sequence(inputs, sort_lens, batch_first=True)
        output, _ = self._lstm_forward(inputs, None)
        _, unsort_idx = torch.sort(sort_idx, dim=0, descending=False)
        output = output[:, unsort_idx]
        return output

    def _lstm_forward(self,
                      inputs: PackedSequence,
                      initial_state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None) -> \
            Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Parameters
        ----------
        inputs : ``PackedSequence``, required.
          A batch first ``PackedSequence`` to run the stacked LSTM over.
        initial_state : ``Tuple[torch.Tensor, torch.Tensor]``, optional, (default = None)
          A tuple (state, memory) representing the initial hidden state and memory
          of the LSTM, with shape (num_layers, batch_size, 2 * hidden_size) and
          (num_layers, batch_size, 2 * cell_size) respectively.
        Returns
        -------
        output_sequence : ``torch.FloatTensor``
          The encoded sequence of shape (num_layers, batch_size, sequence_length, hidden_size)
        final_states: ``Tuple[torch.FloatTensor, torch.FloatTensor]``
          The per-layer final (state, memory) states of the LSTM, with shape
          (num_layers, batch_size, 2 * hidden_size) and  (num_layers, batch_size, 2 * cell_size)
          respectively. The last dimension is duplicated because it contains the state/memory
          for both the forward and backward layers.
        """

        if initial_state is None:
            hidden_states: List[Optional[Tuple[torch.Tensor,
                                               torch.Tensor]]] = [None] * len(self.forward_layers)
        elif initial_state[0].size()[0] != len(self.forward_layers):
            raise Exception("Initial states were passed to forward() but the number of "
                            "initial states does not match the number of layers.")
        else:
            hidden_states = list(zip(initial_state[0].split(1, 0), initial_state[1].split(1, 0)))

        inputs, batch_lengths = pad_packed_sequence(inputs, batch_first=True)
        forward_output_sequence = inputs
        backward_output_sequence = inputs

        final_states = []
        sequence_outputs = []
        for layer_index, state in enumerate(hidden_states):
            forward_layer = getattr(self, 'forward_layer_{}'.format(layer_index))
            backward_layer = getattr(self, 'backward_layer_{}'.format(layer_index))

            forward_cache = forward_output_sequence
            backward_cache = backward_output_sequence

            if state is not None:
                forward_hidden_state, backward_hidden_state = state[0].split(self.hidden_size, 2)
                forward_memory_state, backward_memory_state = state[1].split(self.cell_size, 2)
                forward_state = (forward_hidden_state, forward_memory_state)
                backward_state = (backward_hidden_state, backward_memory_state)
            else:
                forward_state = None
                backward_state = None

            forward_output_sequence, forward_state = forward_layer(forward_output_sequence,
                                                                   batch_lengths,
                                                                   forward_state)
            backward_output_sequence, backward_state = backward_layer(backward_output_sequence,
                                                                      batch_lengths,
                                                                      backward_state)
            # Skip connections, just adding the input to the output.
            if layer_index != 0:
                forward_output_sequence += forward_cache
                backward_output_sequence += backward_cache

            sequence_outputs.append(torch.cat([forward_output_sequence,
                                               backward_output_sequence], -1))
            # Append the state tuples in a list, so that we can return
            # the final states for all the layers.
            final_states.append((torch.cat([forward_state[0], backward_state[0]], -1),
                                 torch.cat([forward_state[1], backward_state[1]], -1)))

        stacked_sequence_outputs: torch.FloatTensor = torch.stack(sequence_outputs)
        # Stack the hidden state and memory for each layer in。to 2 tensors of shape
        # (num_layers, batch_size, hidden_size) and (num_layers, batch_size, cell_size)
        # respectively.
        final_hidden_states, final_memory_states = zip(*final_states)
        final_state_tuple: Tuple[torch.FloatTensor,
                                 torch.FloatTensor] = (torch.cat(final_hidden_states, 0),
                                                       torch.cat(final_memory_states, 0))
        return stacked_sequence_outputs, final_state_tuple

    def load_weights(self, weight_file: str) -> None:
        """
        Load the pre-trained weights from the file.
        """
        requires_grad = False

        with h5py.File(weight_file, 'r') as fin:
            for i_layer, lstms in enumerate(
                    zip(self.forward_layers, self.backward_layers)
            ):
                for j_direction, lstm in enumerate(lstms):
                    # lstm is an instance of LSTMCellWithProjection
                    cell_size = lstm.cell_size

                    dataset = fin['RNN_%s' % j_direction]['RNN']['MultiRNNCell']['Cell%s' % i_layer
                                                                                 ]['LSTMCell']

                    # tensorflow packs together both W and U matrices into one matrix,
                    # but pytorch maintains individual matrices.  In addition, tensorflow
                    # packs the gates as input, memory, forget, output but pytorch
                    # uses input, forget, memory, output.  So we need to modify the weights.
                    tf_weights = numpy.transpose(dataset['W_0'][...])
                    torch_weights = tf_weights.copy()

                    # split the W from U matrices
                    input_size = lstm.input_size
                    input_weights = torch_weights[:, :input_size]
                    recurrent_weights = torch_weights[:, input_size:]
                    tf_input_weights = tf_weights[:, :input_size]
                    tf_recurrent_weights = tf_weights[:, input_size:]

                    # handle the different gate order convention
                    for torch_w, tf_w in [[input_weights, tf_input_weights],
                                          [recurrent_weights, tf_recurrent_weights]]:
                        torch_w[(1 * cell_size):(2 * cell_size), :] = tf_w[(2 * cell_size):(3 * cell_size), :]
                        torch_w[(2 * cell_size):(3 * cell_size), :] = tf_w[(1 * cell_size):(2 * cell_size), :]

                    lstm.input_linearity.weight.data.copy_(torch.FloatTensor(input_weights))
                    lstm.state_linearity.weight.data.copy_(torch.FloatTensor(recurrent_weights))
                    lstm.input_linearity.weight.requires_grad = requires_grad
                    lstm.state_linearity.weight.requires_grad = requires_grad

                    # the bias weights
                    tf_bias = dataset['B'][...]
                    # tensorflow adds 1.0 to forget gate bias instead of modifying the
                    # parameters...
                    tf_bias[(2 * cell_size):(3 * cell_size)] += 1
                    torch_bias = tf_bias.copy()
                    torch_bias[(1 * cell_size):(2 * cell_size)
                    ] = tf_bias[(2 * cell_size):(3 * cell_size)]
                    torch_bias[(2 * cell_size):(3 * cell_size)
                    ] = tf_bias[(1 * cell_size):(2 * cell_size)]
                    lstm.state_linearity.bias.data.copy_(torch.FloatTensor(torch_bias))
                    lstm.state_linearity.bias.requires_grad = requires_grad

                    # the projection weights
                    proj_weights = numpy.transpose(dataset['W_P_0'][...])
                    lstm.state_projection.weight.data.copy_(torch.FloatTensor(proj_weights))
                    lstm.state_projection.weight.requires_grad = requires_grad


class LstmTokenEmbedder(nn.Module):
    def __init__(self, config, word_emb_layer, char_emb_layer):
        super(LstmTokenEmbedder, self).__init__()
        self.config = config
        self.word_emb_layer = word_emb_layer
        self.char_emb_layer = char_emb_layer
        self.output_dim = config['encoder']['projection_dim']
        emb_dim = 0
        if word_emb_layer is not None:
            emb_dim += word_emb_layer.n_d

        if char_emb_layer is not None:
            emb_dim += char_emb_layer.n_d * 2
            self.char_lstm = nn.LSTM(char_emb_layer.n_d, char_emb_layer.n_d, num_layers=1, bidirectional=True,
                                     batch_first=True, dropout=config['dropout'])

        self.projection = nn.Linear(emb_dim, self.output_dim, bias=True)

    def forward(self, words, chars):
        embs = []
        if self.word_emb_layer is not None:
            if hasattr(self, 'words_to_words'):
                words = self.words_to_words[words]
            word_emb = self.word_emb_layer(words)
            embs.append(word_emb)

        if self.char_emb_layer is not None:
            batch_size, seq_len, _ = chars.shape
            chars = chars.view(batch_size * seq_len, -1)
            chars_emb = self.char_emb_layer(chars)
            # TODO 这里应该要考虑seq_len的问题
            _, (chars_outputs, __) = self.char_lstm(chars_emb)
            chars_outputs = chars_outputs.contiguous().view(-1, self.config['token_embedder']['embedding']['dim'] * 2)
            embs.append(chars_outputs)

        token_embedding = torch.cat(embs, dim=2)

        return self.projection(token_embedding)


class ConvTokenEmbedder(nn.Module):
    def __init__(self, config, weight_file, word_emb_layer, char_emb_layer, char_vocab):
        super(ConvTokenEmbedder, self).__init__()
        self.weight_file = weight_file
        self.word_emb_layer = word_emb_layer
        self.char_emb_layer = char_emb_layer

        self.output_dim = config['encoder']['projection_dim']
        self._options = config
        self.requires_grad = False
        self._load_weights()
        self._char_embedding_weights = char_emb_layer.weight.data

    def _load_weights(self):
        self._load_cnn_weights()
        self._load_highway()
        self._load_projection()

    def _load_cnn_weights(self):
        cnn_options = self._options['token_embedder']
        filters = cnn_options['filters']
        char_embed_dim = cnn_options['embedding']['dim']

        convolutions = []
        for i, (width, num) in enumerate(filters):
            conv = torch.nn.Conv1d(
                in_channels=char_embed_dim,
                out_channels=num,
                kernel_size=width,
                bias=True
            )
            # load the weights
            with h5py.File(self.weight_file, 'r') as fin:
                weight = fin['CNN']['W_cnn_{}'.format(i)][...]
                bias = fin['CNN']['b_cnn_{}'.format(i)][...]

            w_reshaped = numpy.transpose(weight.squeeze(axis=0), axes=(2, 1, 0))
            if w_reshaped.shape != tuple(conv.weight.data.shape):
                raise ValueError("Invalid weight file")
            conv.weight.data.copy_(torch.FloatTensor(w_reshaped))
            conv.bias.data.copy_(torch.FloatTensor(bias))

            conv.weight.requires_grad = self.requires_grad
            conv.bias.requires_grad = self.requires_grad

            convolutions.append(conv)
            self.add_module('char_conv_{}'.format(i), conv)

        self._convolutions = convolutions

    def _load_highway(self):
        # the highway layers have same dimensionality as the number of cnn filters
        cnn_options = self._options['token_embedder']
        filters = cnn_options['filters']
        n_filters = sum(f[1] for f in filters)
        n_highway = cnn_options['n_highway']

        # create the layers, and load the weights
        self._highways = Highway(n_filters, n_highway, activation=torch.nn.functional.relu)
        for k in range(n_highway):
            # The AllenNLP highway is one matrix multplication with concatenation of
            # transform and carry weights.
            with h5py.File(self.weight_file, 'r') as fin:
                # The weights are transposed due to multiplication order assumptions in tf
                # vs pytorch (tf.matmul(X, W) vs pytorch.matmul(W, X))
                w_transform = numpy.transpose(fin['CNN_high_{}'.format(k)]['W_transform'][...])
                # -1.0 since AllenNLP is g * x + (1 - g) * f(x) but tf is (1 - g) * x + g * f(x)
                w_carry = -1.0 * numpy.transpose(fin['CNN_high_{}'.format(k)]['W_carry'][...])
                weight = numpy.concatenate([w_transform, w_carry], axis=0)
                self._highways._layers[k].weight.data.copy_(torch.FloatTensor(weight))
                self._highways._layers[k].weight.requires_grad = self.requires_grad

                b_transform = fin['CNN_high_{}'.format(k)]['b_transform'][...]
                b_carry = -1.0 * fin['CNN_high_{}'.format(k)]['b_carry'][...]
                bias = numpy.concatenate([b_transform, b_carry], axis=0)
                self._highways._layers[k].bias.data.copy_(torch.FloatTensor(bias))
                self._highways._layers[k].bias.requires_grad = self.requires_grad

    def _load_projection(self):
        cnn_options = self._options['token_embedder']
        filters = cnn_options['filters']
        n_filters = sum(f[1] for f in filters)

        self._projection = torch.nn.Linear(n_filters, self.output_dim, bias=True)
        with h5py.File(self.weight_file, 'r') as fin:
            weight = fin['CNN_proj']['W_proj'][...]
            bias = fin['CNN_proj']['b_proj'][...]
            self._projection.weight.data.copy_(torch.FloatTensor(numpy.transpose(weight)))
            self._projection.bias.data.copy_(torch.FloatTensor(bias))

            self._projection.weight.requires_grad = self.requires_grad
            self._projection.bias.requires_grad = self.requires_grad

    def forward(self, words, chars):
        """
        :param words:
        :param chars: Tensor  Shape ``(batch_size, sequence_length, 50)``:
        :return Tensor Shape ``(batch_size, sequence_length + 2, embedding_dim)`` :
        """
        # the character id embedding
        # (batch_size * sequence_length, max_chars_per_token, embed_dim)
        # character_embedding = torch.nn.functional.embedding(
        #     chars.view(-1, max_chars_per_token),
        #     self._char_embedding_weights
        # )
        batch_size, sequence_length, max_char_len = chars.size()
        character_embedding = self.char_emb_layer(chars).reshape(batch_size*sequence_length, max_char_len, -1)
        # run convolutions
        cnn_options = self._options['token_embedder']
        if cnn_options['activation'] == 'tanh':
            activation = torch.tanh
        elif cnn_options['activation'] == 'relu':
            activation = torch.nn.functional.relu
        else:
            raise Exception("Unknown activation")

        # (batch_size * sequence_length, embed_dim, max_chars_per_token)
        character_embedding = torch.transpose(character_embedding, 1, 2)
        convs = []
        for i in range(len(self._convolutions)):
            conv = getattr(self, 'char_conv_{}'.format(i))
            convolved = conv(character_embedding)
            # (batch_size * sequence_length, n_filters for this width)
            convolved, _ = torch.max(convolved, dim=-1)
            convolved = activation(convolved)
            convs.append(convolved)

        # (batch_size * sequence_length, n_filters)
        token_embedding = torch.cat(convs, dim=-1)

        # apply the highway layers (batch_size * sequence_length, n_filters)
        token_embedding = self._highways(token_embedding)

        # final projection  (batch_size * sequence_length, embedding_dim)
        token_embedding = self._projection(token_embedding)

        # reshape to (batch_size, sequence_length+2, embedding_dim)
        return token_embedding.view(batch_size, sequence_length, -1)


class Highway(torch.nn.Module):
    """
    A `Highway layer <https://arxiv.org/abs/1505.00387>`_ does a gated combination of a linear
    transformation and a non-linear transformation of its input.  :math:`y = g * x + (1 - g) *
    f(A(x))`, where :math:`A` is a linear transformation, :math:`f` is an element-wise
    non-linearity, and :math:`g` is an element-wise gate, computed as :math:`sigmoid(B(x))`.
    This module will apply a fixed number of highway layers to its input, returning the final
    result.
    Parameters
    ----------
    input_dim : ``int``
        The dimensionality of :math:`x`.  We assume the input has shape ``(batch_size,
        input_dim)``.
    num_layers : ``int``, optional (default=``1``)
        The number of highway layers to apply to the input.
    activation : ``Callable[[torch.Tensor], torch.Tensor]``, optional (default=``torch.nn.functional.relu``)
        The non-linearity to use in the highway layers.
    """

    def __init__(self,
                 input_dim: int,
                 num_layers: int = 1,
                 activation: Callable[[torch.Tensor], torch.Tensor] = torch.nn.functional.relu) -> None:
        super(Highway, self).__init__()
        self._input_dim = input_dim
        self._layers = torch.nn.ModuleList([torch.nn.Linear(input_dim, input_dim * 2)
                                            for _ in range(num_layers)])
        self._activation = activation
        for layer in self._layers:
            # We should bias the highway layer to just carry its input forward.  We do that by
            # setting the bias on `B(x)` to be positive, because that means `g` will be biased to
            # be high, to we will carry the input forward.  The bias on `B(x)` is the second half
            # of the bias vector in each Linear layer.
            layer.bias[input_dim:].data.fill_(1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:  # pylint: disable=arguments-differ
        current_input = inputs
        for layer in self._layers:
            projected_input = layer(current_input)
            linear_part = current_input
            # NOTE: if you modify this, think about whether you should modify the initialization
            # above, too.
            nonlinear_part = projected_input[:, (0 * self._input_dim):(1 * self._input_dim)]
            gate = projected_input[:, (1 * self._input_dim):(2 * self._input_dim)]
            nonlinear_part = self._activation(nonlinear_part)
            gate = torch.sigmoid(gate)
            current_input = gate * linear_part + (1 - gate) * nonlinear_part
        return current_input


class _ElmoModel(nn.Module):
    """
    该Module是ElmoEmbedding中进行所有的heavy lifting的地方。做的工作，包括
        (1) 根据配置，加载模型;
        (2) 根据vocab，对模型中的embedding进行调整. 并将其正确初始化
        (3) 保存一个words与chars的对应转换，获取时自动进行相应的转换
        (4) 设计一个保存token的embedding，允许缓存word的表示。

    """

    def __init__(self, model_dir: str, vocab: Vocabulary = None, cache_word_reprs: bool = False):
        super(_ElmoModel, self).__init__()

        dir = os.walk(model_dir)
        config_file = None
        weight_file = None
        config_count = 0
        weight_count = 0
        for path, dir_list, file_list in dir:
            for file_name in file_list:
                if file_name.__contains__(".json"):
                    config_file = file_name
                    config_count += 1
                elif file_name.__contains__(".hdf5"):
                    weight_file = file_name
                    weight_count += 1
        if config_count > 1 or weight_count > 1:
            raise Exception(f"Multiple config files(*.json) or weight files(*.hdf5) detected in {model_dir}.")
        elif config_count == 0 or weight_count == 0:
            raise Exception(f"No config file or weight file found in {model_dir}")

        config = json.load(open(os.path.join(model_dir, config_file), 'r'))
        self.weight_file = os.path.join(model_dir, weight_file)
        self.config = config
        self.requires_grad = False

        OOV_TAG = '<oov>'
        PAD_TAG = '<pad>'
        BOS_TAG = '<bos>'
        EOS_TAG = '<eos>'
        BOW_TAG = '<bow>'
        EOW_TAG = '<eow>'

        # For the model trained with character-based word encoder.
        if config['token_embedder']['embedding']['dim'] > 0:
            char_lexicon = {}
            with codecs.open(os.path.join(model_dir, 'char.dic'), 'r', encoding='utf-8') as fpi:
                for line in fpi:
                    tokens = line.strip().split('\t')
                    if len(tokens) == 1:
                        tokens.insert(0, '\u3000')
                    token, i = tokens
                    char_lexicon[token] = int(i)

            # 做一些sanity check
            for special_word in [PAD_TAG, OOV_TAG, BOW_TAG, EOW_TAG]:
                assert special_word in char_lexicon, f"{special_word} not found in char.dic."

            # 从vocab中构建char_vocab
            char_vocab = Vocabulary(unknown=OOV_TAG, padding=PAD_TAG)
            # 需要保证<bow>与<eow>在里面
            char_vocab.add_word_lst([BOW_TAG, EOW_TAG, BOS_TAG, EOS_TAG])

            for word, index in vocab:
                char_vocab.add_word_lst(list(word))

            self.bos_index, self.eos_index, self._pad_index = len(vocab), len(vocab)+1, vocab.padding_idx
            # 根据char_lexicon调整, 多设置一位，是预留给word padding的(该位置的char表示为全0表示)
            char_emb_layer = nn.Embedding(len(char_vocab)+1, int(config['token_embedder']['embedding']['dim']),
                                          padding_idx=len(char_vocab))
            with h5py.File(self.weight_file, 'r') as fin:
                char_embed_weights = fin['char_embed'][...]
            char_embed_weights = torch.from_numpy(char_embed_weights)
            found_char_count = 0
            for char, index in char_vocab:  # 调整character embedding
                if char in char_lexicon:
                    index_in_pre = char_lexicon.get(char)
                    found_char_count += 1
                else:
                    index_in_pre = char_lexicon[OOV_TAG]
                char_emb_layer.weight.data[index] = char_embed_weights[index_in_pre]

            print(f"{found_char_count} out of {len(char_vocab)} characters were found in pretrained elmo embedding.")
            # 生成words到chars的映射
            if config['token_embedder']['name'].lower() == 'cnn':
                max_chars = config['token_embedder']['max_characters_per_token']
            elif config['token_embedder']['name'].lower() == 'lstm':
                max_chars = max(map(lambda x: len(x[0]), vocab)) + 2  # 需要补充两个<bow>与<eow>
            else:
                raise ValueError('Unknown token_embedder: {0}'.format(config['token_embedder']['name']))

            self.words_to_chars_embedding = nn.Parameter(torch.full((len(vocab)+2, max_chars),
                                                                    fill_value=len(char_vocab),
                                                                    dtype=torch.long),
                                                         requires_grad=False)
            for word, index in list(iter(vocab)) + [(BOS_TAG, len(vocab)), (EOS_TAG, len(vocab)+1)]:
                if len(word) + 2 > max_chars:
                    word = word[:max_chars - 2]
                if index == self._pad_index:
                    continue
                elif word == BOS_TAG or word == EOS_TAG:
                    char_ids = [char_vocab.to_index(BOW_TAG)] + [char_vocab.to_index(word)] + [
                        char_vocab.to_index(EOW_TAG)]
                    char_ids += [char_vocab.to_index(PAD_TAG)] * (max_chars - len(char_ids))
                else:
                    char_ids = [char_vocab.to_index(BOW_TAG)] + [char_vocab.to_index(c) for c in word] + [
                        char_vocab.to_index(EOW_TAG)]
                    char_ids += [char_vocab.to_index(PAD_TAG)] * (max_chars - len(char_ids))
                self.words_to_chars_embedding[index] = torch.LongTensor(char_ids)

            self.char_vocab = char_vocab
        else:
            char_emb_layer = None

        if config['token_embedder']['name'].lower() == 'cnn':
            self.token_embedder = ConvTokenEmbedder(
                config, self.weight_file, None, char_emb_layer, self.char_vocab)
        elif config['token_embedder']['name'].lower() == 'lstm':
            self.token_embedder = LstmTokenEmbedder(
                config, None, char_emb_layer)

        if config['token_embedder']['word_dim'] > 0 \
                and vocab._no_create_word_length > 0:  # 需要映射，使得来自于dev, test的idx指向unk
            words_to_words = nn.Parameter(torch.arange(len(vocab) + 2).long(), requires_grad=False)
            for word, idx in vocab:
                if vocab._is_word_no_create_entry(word):
                    words_to_words[idx] = vocab.unknown_idx
            setattr(self.token_embedder, 'words_to_words', words_to_words)
        self.output_dim = config['encoder']['projection_dim']

        # 暂时只考虑 elmo
        if config['encoder']['name'].lower() == 'elmo':
            self.encoder = ElmobiLm(config)
        elif config['encoder']['name'].lower() == 'lstm':
            self.encoder = LstmbiLm(config)

        self.encoder.load_weights(self.weight_file)

        if cache_word_reprs:
            if config['token_embedder']['embedding']['dim'] > 0:  # 只有在使用了chars的情况下有用
                print("Start to generate cache word representations.")
                batch_size = 320
                # bos eos
                word_size = self.words_to_chars_embedding.size(0)
                num_batches = word_size // batch_size + \
                              int(word_size % batch_size != 0)

                self.cached_word_embedding = nn.Embedding(word_size,
                                                          config['encoder']['projection_dim'])
                with torch.no_grad():
                    for i in range(num_batches):
                        words = torch.arange(i * batch_size,
                                             min((i + 1) * batch_size, word_size)).long()
                        chars = self.words_to_chars_embedding[words].unsqueeze(1)  # batch_size x 1 x max_chars
                        word_reprs = self.token_embedder(words.unsqueeze(1),
                                                         chars).detach()  # batch_size x 1 x config['encoder']['projection_dim']
                        self.cached_word_embedding.weight.data[words] = word_reprs.squeeze(1)

                    print("Finish generating cached word representations. Going to delete the character encoder.")
                del self.token_embedder, self.words_to_chars_embedding
            else:
                print("There is no need to cache word representations, since no character information is used.")

    def forward(self, words):
        """

        :param words: batch_size x max_len
        :return: num_layers x batch_size x max_len x hidden_size
        """
        # 扩展<bos>, <eos>
        batch_size, max_len = words.size()
        expanded_words = words.new_zeros(batch_size, max_len + 2)  # 因为pad一定为0，
        seq_len = words.ne(self._pad_index).sum(dim=-1)
        expanded_words[:, 1:-1] = words
        expanded_words[:, 0].fill_(self.bos_index)
        expanded_words[torch.arange(batch_size).to(words), seq_len + 1] = self.eos_index
        seq_len = seq_len + 2
        if hasattr(self, 'cached_word_embedding'):
            token_embedding = self.cached_word_embedding(expanded_words)
        else:
            if hasattr(self, 'words_to_chars_embedding'):
                chars = self.words_to_chars_embedding[expanded_words]
            else:
                chars = None
            token_embedding = self.token_embedder(expanded_words, chars)  # batch_size x max_len x embed_dim

        if self.config['encoder']['name'] == 'elmo':
            encoder_output = self.encoder(token_embedding, seq_len)
            if encoder_output.size(2) < max_len + 2:
                num_layers, _, output_len, hidden_size = encoder_output.size()
                dummy_tensor = encoder_output.new_zeros(num_layers, batch_size,
                                                        max_len + 2 - output_len, hidden_size)
                encoder_output = torch.cat((encoder_output, dummy_tensor), 2)
            sz = encoder_output.size() # 2, batch_size, max_len, hidden_size
            token_embedding = torch.cat((token_embedding, token_embedding), dim=2).view(1, sz[1], sz[2], sz[3])
            encoder_output = torch.cat((token_embedding, encoder_output), dim=0)
        elif self.config['encoder']['name'] == 'lstm':
            encoder_output = self.encoder(token_embedding, seq_len)
        else:
            raise ValueError('Unknown encoder: {0}'.format(self.config['encoder']['name']))

        # 删除<eos>, <bos>. 这里没有精确地删除，但应该也不会影响最后的结果了。
        encoder_output = encoder_output[:, :, 1:-1]
        return encoder_output
