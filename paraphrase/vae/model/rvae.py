import numpy as np
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from .decoder import Decoder
from .encoder import Encoder

from selfModules.embedding import Embedding

from utils.functional import kld_coef, parameters_allocation_check, fold
from beam_search import Beam


class RVAE(nn.Module):
    def __init__(self, params, params_2):
        super(RVAE, self).__init__()

        self.params = params
        self.params_2 = params_2  # Encoder-2 parameters

        self.embedding = Embedding(self.params, '')
        self.embedding_2 = Embedding(self.params_2, '', True)

        self.encoder = Encoder(self.params)
        self.encoder_2 = Encoder(self.params_2)

        #
        self.context_to_mu = nn.Linear(self.params.encoder_rnn_size * 2, self.params.latent_variable_size)
        self.context_to_logvar = nn.Linear(self.params.encoder_rnn_size * 2, self.params.latent_variable_size)

        # self.encoder_3 = Encoder(self.params)
        self.decoder = Decoder(self.params_2)  # change this to params_2

    def forward(self, drop_prob,
                encoder_word_input=None, encoder_character_input=None,
                encoder_word_input_2=None, encoder_character_input_2=None,
                decoder_word_input_2=None, decoder_character_input_2=None,
                z=None, initial_state=None):

        assert parameters_allocation_check(self)
        use_cuda = self.embedding.word_embed.weight.is_cuda

        assert z is None and fold(lambda acc, parameter: acc and parameter is not None,
                                  [encoder_word_input, encoder_character_input, decoder_word_input_2],
                                  True) \
               or (z is not None and decoder_word_input_2 is not None)

        if z is None:
            [batch_size, _] = encoder_word_input.size()

            encoder_input = self.embedding(encoder_word_input, encoder_character_input)

            [batch_size_2, _] = encoder_word_input_2.size()

            encoder_input_2 = self.embedding_2(encoder_word_input_2, encoder_character_input_2)

            context, h_0, c_0 = self.encoder(encoder_input, None)

            State = (h_0, c_0)
            context_2, _, _ = self.encoder_2(encoder_input_2, State)

            mu = self.context_to_mu(context_2)
            logvar = self.context_to_logvar(context_2)
            std = t.exp(0.5 * logvar)

            z = Variable(t.randn([batch_size, self.params.latent_variable_size]))
            if use_cuda:
                z = z.cuda()

            z = z * std + mu

            kld = (-0.5 * t.sum(logvar - t.pow(mu, 2) - t.exp(logvar) + 1, 1)).mean().squeeze()

            # encoder_input = self.embedding(encoder_word_input, encoder_character_input)
            # _ , h_0 , c_0 = self.encoder_3(encoder_input, None)
            initial_state = State

        else:
            kld = None
            mu = None
            std = None

        decoder_input_2 = self.embedding_2.word_embed(decoder_word_input_2)
        out, final_state = self.decoder(decoder_input_2, z, drop_prob, initial_state)

        return out, final_state, kld, mu, std

    def learnable_parameters(self):
        return [p for p in self.parameters() if p.requires_grad]

    def trainer(self, optimizer, batch_loader, batch_loader_2):
        def train(i, batch_size, use_cuda, dropout, start_index):
            input = batch_loader.next_batch(batch_size, 'train', start_index)
            input = [Variable(t.from_numpy(var)) for var in input]
            input = [var.long() for var in input]
            input = [var.cuda() if use_cuda else var for var in input]
            [encoder_word_input, encoder_character_input, decoder_word_input, decoder_character_input, target] = input

            input_2 = batch_loader_2.next_batch(batch_size, 'train', start_index)
            input_2 = [Variable(t.from_numpy(var)) for var in input_2]
            input_2 = [var.long() for var in input_2]
            input_2 = [var.cuda() if use_cuda else var for var in input_2]
            [encoder_word_input_2, encoder_character_input_2, decoder_word_input_2, decoder_character_input_2,
             target] = input_2

            logits, _, kld, _, _ = self(dropout,
                                        encoder_word_input, encoder_character_input,
                                        encoder_word_input_2, encoder_character_input_2,
                                        decoder_word_input_2, decoder_character_input_2,
                                        z=None)

            # logits = logits.view(-1, self.params.word_vocab_size)
            logits = logits.view(-1, self.params_2.word_vocab_size)
            target = target.view(-1)
            cross_entropy = F.cross_entropy(logits, target)
            loss = 79 * cross_entropy + kld_coef(i) * kld
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            return cross_entropy, kld, kld_coef(i)

        return train

    def validater(self, batch_loader, batch_loader_2):
        def validate(batch_size, use_cuda, start_index):
            input = batch_loader.next_batch(batch_size, 'valid', start_index)
            input = [Variable(t.from_numpy(var)) for var in input]
            input = [var.long() for var in input]
            input = [var.cuda() if use_cuda else var for var in input]

            [encoder_word_input, encoder_character_input, decoder_word_input, decoder_character_input, target] = input

            input_2 = batch_loader_2.next_batch(batch_size, 'valid', start_index)
            input_2 = [Variable(t.from_numpy(var)) for var in input_2]
            input_2 = [var.long() for var in input_2]
            input_2 = [var.cuda() if use_cuda else var for var in input_2]
            [encoder_word_input_2, encoder_character_input_2, decoder_word_input_2, decoder_character_input_2,
             target] = input_2

            logits, _, kld, _, _ = self(0.,
                                        encoder_word_input, encoder_character_input,
                                        encoder_word_input_2, encoder_character_input_2,
                                        decoder_word_input_2, decoder_character_input_2,
                                        z=None)
            logits = logits.view(-1, self.params_2.word_vocab_size)
            target = target.view(-1)
            cross_entropy = F.cross_entropy(logits, target)

            return cross_entropy, kld

        return validate

    def sample(self, batch_loader, seq_len, seed, use_cuda, State):
        if use_cuda:
            seed = seed.cuda()

        decoder_word_input_np, decoder_character_input_np = batch_loader.go_input(1)

        decoder_word_input = Variable(t.from_numpy(decoder_word_input_np).long())
        decoder_character_input = Variable(t.from_numpy(decoder_character_input_np).long())

        if use_cuda:
            decoder_word_input, decoder_character_input = decoder_word_input.cuda(), decoder_character_input.cuda()

        result = ''

        initial_state = State

        for i in range(seq_len):
            logits, initial_state, _, _, _ = self(0., None, None,
                                                  None, None,
                                                  decoder_word_input, decoder_character_input,
                                                  seed, initial_state)

            # forward(self, drop_prob,
            #           encoder_word_input=None, encoder_character_input=None,
            #           encoder_word_input_2=None, encoder_character_input_2=None,
            #           decoder_word_input_2=None, decoder_character_input_2=None,
            #           z=None, initial_state=None):

            # logits = logits.view(-1, self.params.word_vocab_size)
            # logits = logits.view(-1, self.params.word_vocab_size)
            logits = logits.view(-1, self.params_2.word_vocab_size)
            # print '---------------------------------------'
            # print 'Printing logits'
            # print logits
            # print '------------------------------------------'

            prediction = F.softmax(logits)

            word = batch_loader.sample_word_from_distribution(prediction.data.cpu().numpy()[-1])

            if word == batch_loader.end_token:
                break

            result += ' ' + word

            decoder_word_input_np = np.array([[batch_loader.word_to_idx[word]]])
            decoder_character_input_np = np.array([[batch_loader.encode_characters(word)]])

            decoder_word_input = Variable(t.from_numpy(decoder_word_input_np).long())
            decoder_character_input = Variable(t.from_numpy(decoder_character_input_np).long())

            if use_cuda:
                decoder_word_input, decoder_character_input = decoder_word_input.cuda(), decoder_character_input.cuda()

        return result

    def sampler(self, batch_loader, batch_loader_2, seq_len, seed, use_cuda, i, beam_size, n_best):
        input = batch_loader.next_batch(1, 'valid', i)
        input = [Variable(t.from_numpy(var)) for var in input]
        input = [var.long() for var in input]
        input = [var.cuda() if use_cuda else var for var in input]
        [encoder_word_input, encoder_character_input, decoder_word_input, decoder_character_input, target] = input

        encoder_input = self.embedding(encoder_word_input, encoder_character_input)

        _, h0, c0 = self.encoder(encoder_input, None)
        State = (h0, c0)

        results, scores = self.sample_beam(batch_loader_2, seq_len, seed, use_cuda, State, beam_size, n_best)

        return results, scores

    def sample_beam(self, batch_loader, seq_len, seed, use_cuda, State, beam_size, n_best):
        # seed = Variable(t.from_numpy(seed).float())
        if use_cuda:
            seed = seed.cuda()

        decoder_word_input_np, decoder_character_input_np = batch_loader.go_input(1)

        decoder_word_input = Variable(t.from_numpy(decoder_word_input_np).long())
        decoder_character_input = Variable(t.from_numpy(decoder_character_input_np).long())

        if use_cuda:
            decoder_word_input, decoder_character_input = decoder_word_input.cuda(), decoder_character_input.cuda()

        dec_states = State

        dec_states = [
            dec_states[0].repeat(1, beam_size, 1),
            dec_states[1].repeat(1, beam_size, 1)
        ]

        drop_prob = 0.0
        beam_size = beam_size
        batch_size = 1

        beam = [Beam(beam_size, batch_loader, cuda=True) for k in range(batch_size)]

        batch_idx = list(range(batch_size))
        remaining_sents = batch_size

        for i in range(seq_len):

            input = t.stack(
                [b.get_current_state() for b in beam if not b.done]
            ).t().contiguous().view(1, -1)

            trg_emb = self.embedding_2.word_embed(Variable(input).transpose(1, 0))

            # print trg_emb.size()
            # print seed.size()

            trg_h, dec_states = self.decoder.only_decoder_beam(trg_emb, seed, drop_prob, dec_states)

            dec_out = trg_h.squeeze(1)

            # print "dec_out:", dec_out.size()

            out = F.softmax(self.decoder.fc(dec_out)).unsqueeze(0)

            word_lk = out.view(
                beam_size,
                remaining_sents,
                -1
            ).transpose(0, 1).contiguous()

            active = []
            for b in range(batch_size):
                if beam[b].done:
                    continue

                idx = batch_idx[b]
                if not beam[b].advance(word_lk.data[idx]):
                    active += [b]

                for dec_state in dec_states:  # iterate over h, c
                    # layers x beam*sent x dim
                    sent_states = dec_state.view(
                        -1, beam_size, remaining_sents, dec_state.size(2)
                    )[:, :, idx]
                    sent_states.data.copy_(
                        sent_states.data.index_select(
                            1,
                            beam[b].get_current_origin()
                        )
                    )

            if not active:
                break

            active_idx = t.cuda.LongTensor([batch_idx[k] for k in active])
            batch_idx = {beam: idx for idx, beam in enumerate(active)}

            def update_active(t):
                view = t.data.view(
                    -1, remaining_sents,
                    self.params.decoder_rnn_size
                )
                new_size = list(t.size())
                new_size[-2] = new_size[-2] * len(active_idx) \
                               // remaining_sents
                return Variable(view.index_select(
                    1, active_idx
                ).view(*new_size))

            dec_states = (
                update_active(dec_states[0]),
                update_active(dec_states[1])
            )
            dec_out = update_active(dec_out)

            remaining_sents = len(active)

        allHyp, allScores = [], []

        for b in range(batch_size):
            scores, ks = beam[b].sort_best()
            allScores += [scores[:n_best]]
            hyps = zip(*[beam[b].get_hyp(k) for k in ks[:n_best]])
            allHyp += [hyps]

        return allHyp, allScores
