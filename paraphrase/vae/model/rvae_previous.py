import numpy as np
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from .decoder import Decoder
from .encoder import Encoder

from selfModules.embedding import Embedding

from utils.functional import kld_coef, parameters_allocation_check, fold


class RVAE(nn.Module):
    def __init__(self, params,params_2):
        super(RVAE, self).__init__()

        self.params = params
        self.params_2 = params_2

        self.embedding = Embedding(self.params, '')
        self.embedding_2 = Embedding(self.params_2, '')

        self.encoder = Encoder(self.params)
        self.encoder_2 = Encoder(self.params_2)


        self.context_to_mu = nn.Linear(self.params.encoder_rnn_size * 2, self.params.latent_variable_size)
        self.context_to_logvar = nn.Linear(self.params.encoder_rnn_size * 2, self.params.latent_variable_size)

        self.encoder_3 = Encoder(self.params)
        self.decoder = Decoder(self.params_2)

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
            
            context , h_0 , c_0 = self.encoder(encoder_input, None)
            
            State = (h_0,c_0)
            context_2 , _ , _ = self.encoder_2( encoder_input_2, State )
            
            mu = self.context_to_mu(context_2)
            logvar = self.context_to_logvar(context_2)
            std = t.exp(0.5 * logvar)

            z = Variable(t.randn([batch_size, self.params.latent_variable_size]))
            if use_cuda:
                z = z.cuda()

            z = z * std + mu

            kld = (-0.5 * t.sum(logvar - t.pow(mu, 2) - t.exp(logvar) + 1, 1)).mean().squeeze()

            encoder_input = self.embedding(encoder_word_input, encoder_character_input)
            _ , h_0 , c_0 = self.encoder_3(encoder_input, None)
            initial_state = (h_0,c_0)

        else:
            kld = None
        decoder_input_2 = self.embedding.word_embed(decoder_word_input_2)
        out, final_state = self.decoder(decoder_input_2, z, drop_prob, initial_state)

        return out, final_state, kld

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

            [encoder_word_input_2, encoder_character_input_2, decoder_word_input_2, decoder_character_input_2, target] = input_2

            logits, _, kld = self(dropout,
                                  encoder_word_input, encoder_character_input,
                                  encoder_word_input_2,encoder_character_input_2,
                                  decoder_word_input_2, decoder_character_input_2,
                                  z=None)

            logits = logits.view(-1, self.params_2.word_vocab_size)
            target = target.view(-1)
            cross_entropy = F.cross_entropy(logits, target)

            loss = 79 * cross_entropy + kld_coef(i) * kld

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            return cross_entropy, kld, kld_coef(i)

        return train

    def validater(self, batch_loader,batch_loader_2):
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
            [encoder_word_input_2, encoder_character_input_2, decoder_word_input_2, decoder_character_input_2, target] = input_2
            logits, _, kld = self(0.,
                                  encoder_word_input, encoder_character_input,
                                  encoder_word_input_2,encoder_character_input_2,
                                  decoder_word_input_2, decoder_character_input_2,
                                  z=None)
            logits = logits.view(-1, self.params_2.word_vocab_size)
            target = target.view(-1)
            cross_entropy = F.cross_entropy(logits, target)

            return cross_entropy, kld

        return validate

    def sample(self, batch_loader, seq_len, seed, use_cuda, State):
        seed = Variable(t.from_numpy(seed).float())
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
            logits, initial_state, _ = self(0., None, None,
                                                None, None,
                                            decoder_word_input, decoder_character_input,
                                            seed, initial_state)

            logits = logits.view(-1, self.params_2.word_vocab_size)
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

    def sampler(self, batch_loader, seq_len, seed, use_cuda):
        input = batch_loader.next_batch(1, 'valid', 1)
        input = [Variable(t.from_numpy(var)) for var in input]
        input = [var.long() for var in input]
        input = [var.cuda() if use_cuda else var for var in input]
        [encoder_word_input, encoder_character_input, decoder_word_input, decoder_character_input, target] = input

        encoder_input = self.embedding(encoder_word_input, encoder_character_input)

        _ , h0 , c0 = self.encoder_3(encoder_input, None)
        State = (h0,c0)
        result = self.sample(batch_loader, seq_len, seed, use_cuda, State)
        return result
