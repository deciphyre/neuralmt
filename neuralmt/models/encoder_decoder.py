#!/usr/bin/env python
# -*- coding: utf-8 -*-

from abc import ABCMeta, abstractmethod

import deepy as D
import deepy.tensor as T
from deepy.trainers import ScheduledLearningRateAnnealer
from deepy.utils import MapDict

from ..core import NeuralMTConfiguration, NeuralTranslator
from ..utils import SimpleBleuValidator

D.debug.check_test_values()

import deepy.layers as L
#
# class TMCostLayer(L.NeuralLayer):
#
#     def __init__(self, target, mask, target_size, cost_map=None):
#         """
#         :param target: 2d (batch, time)
#         :type target: NeuralVariable
#         :param mask:  2d (batch, time)
#         :type mask: NeuralVariable
#         :param target_size: scalar
#         """
#         super(TMCostLayer, self).__init__("tm_cost")
#         self.target = target.tensor
#         self.mask = mask.tensor
#         self.target_size = target_size
#         self.cost_map = cost_map
#
#     def compute_tensor(self, x):
#         """
#         :param x: 3d tensor (batch, time, vocab)
#         """
#         flat_mask = self.mask.flatten()
#
#         # Softmax
#         shape = x.shape
#         x = x.reshape((shape[0] * shape[1], shape[2])) * flat_mask[:, None]
#         softmax_tensor = T.nnet.softmax(x)
#
#         # Get cost
#         result_vector = softmax_tensor.flatten()
#         target_vector = self.target.flatten()
#         target_index_vector = T.arange(target_vector.shape[0]) * self.target_size + target_vector
#
#         prob_vector = result_vector[target_index_vector]
#         prob_vector = T.clip(prob_vector, D.env.EPSILON, 1.0 - D.env.EPSILON)
#         log_prob_vector = - T.log(prob_vector) * flat_mask
#         if self.cost_map:
#             log_prob_vector *= self.cost_map.flatten()
#         cost = T.sum(log_prob_vector) / T.sum(flat_mask)
#         return cost

class EncoderDecoderModel(object):
    __metaclass__ = ABCMeta

    def __init__(self, hidden_size=1000, embed_size=1000, src_vocab_size=80000, tgt_vocab_size=40000, decoder_states=None):
        self._hidden_size = hidden_size
        self._embed_size = embed_size
        self._src_vocab_size = src_vocab_size
        self._tgt_vocab_size = tgt_vocab_size
        self._decoder_states = decoder_states if decoder_states else["state", "cell"]
        self._layers = []
        self.prepare()

    @abstractmethod
    def prepare(self):
        """
        Create layers.
        """

    @abstractmethod
    def encode(self, input_vars, input_mask=None):
        """
        Encode input sequence and return a value map.
        """

    @abstractmethod
    def lookup_feedback(self, feedback):
        """
        Get the word embeddings of feedback tokens.
        """

    @abstractmethod
    def decode_step(self, vars):
        """
        Computations of each decoding step.
        """

    @abstractmethod
    def expand(self, decoder_outputs):
        """
        Expand decoder outputs to a vocab-size tensor.
        """

    def sample_step(self, vars):
        """
        Decode step for valiadation.
        """
        sampled_tokens = self.expand(vars).argmax(axis=1)
        sampled_embed = self.lookup_feedback(sampled_tokens)
        feedback_embed = T.ifelse(vars.t == 0, vars.feedback, sampled_embed)
        vars.feedback = feedback_embed
        self.decode_step(vars)

    def decode(self, encoder_outputs, target_vars, input_mask=None, sampling=False):
        """
        Decoding graph.
        """
        encoder_states = encoder_outputs.encoder_states
        batch_size = encoder_states.shape[0]
        feedbacks = T.concat([T.ones((batch_size, 1), dtype="int32"), target_vars[:, :-1]], axis=1)
        feedback_embeds = self.lookup_feedback(feedbacks)

        # Process initial states
        decoder_outputs = {"t": T.constant(0)}
        for state_name in self._decoder_states:
            if "init_{}".format(state_name) in encoder_outputs:
                decoder_outputs[state_name] = encoder_outputs["init_{}".format(state_name)]
            else:
                decoder_outputs[state_name] = T.zeros((batch_size, self._hidden_size))
        # Process non-seqeuences
        non_sequences = {"input_mask": input_mask}
        for k, val in encoder_outputs.items():
            if not k.startswith("init_"):
                non_sequences[k] = val
        loop = D.graph.loop(
            sequences={"feedback": feedback_embeds.dimshuffle((1, 0, 2))},
            outputs=decoder_outputs,
            non_sequences=non_sequences)
        with loop as vars:
            if sampling:
                self.sample_step(vars)
            else:
                self.decode_step(vars)
            vars.t += 1
        decoder_outputs = MapDict()
        for state_name in self._decoder_states:
            decoder_outputs[state_name] = loop.outputs[state_name].dimshuffle((1, 0, 2))
        return decoder_outputs

    def compile_train(self):
        """
        Get training graph.
        """
        src_vars, src_mask, tgt_vars, tgt_mask = T.vars('imatrix', 'matrix', 'imatrix', 'matrix')
        encoder_outputs = MapDict(self.encode(src_vars, src_mask))
        decoder_outputs = self.decode(encoder_outputs, tgt_vars, input_mask=src_mask)
        output_vars = self.expand(decoder_outputs)

        cost = T.costs.cross_entropy(output_vars, tgt_vars, mask=tgt_mask)
        # cost = TMCostLayer(tgt_vars, tgt_mask, self._tgt_vocab_size).compute(output_vars)
        accuracy = T.costs.accuracy(output_vars.argmax(axis=2), tgt_vars, mask=tgt_mask)
        model_params = D.graph.new_block(*self._layers)
        return D.graph.compile(input_vars=[src_vars, src_mask, tgt_vars, tgt_mask],
                               blocks=[model_params],
                               cost=cost,
                               monitors={"acc": accuracy})

    def compile_valid(self):
        """
        Get validation graph.
        """
        src_vars, src_mask, tgt_vars, tgt_mask = T.vars('imatrix', 'matrix', 'imatrix', 'matrix')
        encoder_outputs = MapDict(self.encode(src_vars, src_mask))
        decoder_outputs = self.decode(encoder_outputs, tgt_vars, input_mask=src_mask)
        sampled_outputs = self.decode(encoder_outputs, tgt_vars, input_mask=src_mask, sampling=True)
        output_vars = self.expand(decoder_outputs)
        sampled_output_vars = self.expand(sampled_outputs)

        cost = T.costs.cross_entropy(output_vars, tgt_vars, mask=tgt_mask)
        accuracy = T.costs.accuracy(output_vars.argmax(axis=2), tgt_vars, mask=tgt_mask)
        return D.graph.compile(input_vars=[src_vars, src_mask, tgt_vars, tgt_mask],
                               cost=cost,
                               outputs={
                                   "acc": accuracy,
                                   "outputs": sampled_output_vars.argmax(axis=2)
                               })

    def decoder_hidden_size(self):
        return self._hidden_size * len(self._decoder_states)

    def load_params(self, path):
        self.compile_train().load_params(path)

    def export_test_components(self):
        """
        Export encoder, decoder and expander for test.
        """
        # Encoder
        input_var = T.var('imatrix')
        encoder_outputs = MapDict(self.encode(input_var))
        encoder_graph = D.graph.compile(input_vars=[input_var], outputs=encoder_outputs)

        # Decoder
        t_var, feedback_var = T.vars('iscalar', 'ivector')
        state_var = T.var('matrix', test_shape=[3, len(self._decoder_states) * self._hidden_size])
        feedback_embeds = self.lookup_feedback(feedback_var)
        feedback_embeds = T.ifelse(t_var == 0, feedback_embeds, feedback_embeds) # Trick to prevent warning of unused inputs
        vars = MapDict({
            "feedback": feedback_embeds,
            "t": t_var
        })
        first_encoder_outputs = MapDict([(k, v[0]) for (k, v) in encoder_outputs.items()])

        for i, state_name in enumerate(self._decoder_states):
            state_val = state_var[:, self._hidden_size * i: self._hidden_size * (i + 1)]
            if "init_{}".format(state_name) in first_encoder_outputs:
                state_val = T.ifelse(t_var == 0, T.repeat(first_encoder_outputs["init_{}".format(state_name)][None, :], state_var.shape[0], axis=0), state_val)
            vars[state_name] = state_val
        vars.update(first_encoder_outputs)
        self.decode_step(vars)

        state_output = T.concatenate([vars[k] for k in self._decoder_states], axis=1)
        decoder_inputs = [t_var, state_var, feedback_var] + [p[1] for p in sorted(first_encoder_outputs.items())]
        decoder_graph = D.graph.compile(input_vars=decoder_inputs, output=state_output)

        # Expander
        decoder_state = T.var('matrix', test_shape=[3, self._hidden_size])
        decoder_outputs = MapDict()
        for i, state_name in enumerate(self._decoder_states):
            decoder_outputs[state_name] = decoder_state[:, self._hidden_size * i: self._hidden_size * (i + 1)]
        prob = T.nnet.softmax(self.expand(decoder_outputs))
        expander_graph = D.graph.compile(input_vars=[decoder_state], output=prob)

        return encoder_graph, decoder_graph, expander_graph


    def get_trainer(self, method='adam', config=None, annealer=None, valid_freq=1500, save_path=None, valid_criteria='bleu'):
        """
        Get a trainer.
        """
        if not annealer:
            annealer = ScheduledLearningRateAnnealer(start_halving_at=3, end_at=6)
        return D.graph.get_trainer(self.compile_train(), method, config,
                                   annealer=annealer,
                                   validator=SimpleBleuValidator(self.compile_valid(), freq=valid_freq, save_path=save_path, criteria=valid_criteria))

    def get_translator(self, source_vocab, target_vocab, model_path=None):
        """
        Get a translator.
        """
        if model_path:
            self.load_params(model_path)
        config = NeuralMTConfiguration(
            target_vocab=target_vocab
        ).add_mt_path(
            source_vocab, self
        )

        return NeuralTranslator(config)