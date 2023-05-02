import math

import torch
from torch import nn
from torch.nn import init
from torch.nn import functional as F

from src.model import basic
import numpy as np
from src.model.scan_data import ScanTypes
from src.model.scan_data import initial_decodings_scan
from src.model.cogs_data import initial_decodings_cogs, initial_variables_cogs, copy_temp_cogs
from src.model.data import int_to_one_hot


class FeedForward(nn.Module):
    def __init__(self, input_dim, hidden_dims, output_dim):
        super().__init__()

        dims = [input_dim] + hidden_dims + [output_dim]
        self.layers = nn.ModuleList()
        for in_d, out_d in zip(dims[:-1], dims[1:]):
            self.layers.append(nn.Linear(in_d, out_d))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = layer(x)
            if i != len(self.layers) - 1:
                x = F.relu(x)
        return x


class TypePredictor(nn.Module):

    def __init__(self, repr_dim, hidden_dims, num_types, gumbel_temp=1.):
        super().__init__()
        self.net = FeedForward(repr_dim, hidden_dims, num_types)
        self.gumbel_temp = gumbel_temp

    def reduce_gumbel_temp(self, factor, iter, verbose=False):
        # TK DEBUG
        new_temp = np.maximum(self.gumbel_temp * np.exp(-factor * iter), 0.5)
        if verbose:
            print(f'Gumbel temp lowered from {self.gumbel_temp:g} to {new_temp:g}')
        self.gumbel_temp = new_temp

    def score(self, x):
        return self.net(x)

    def sample(self, type_scores):
        distribution = F.gumbel_softmax(type_scores, tau=self.gumbel_temp, dim=-1)
        samples = torch.argmax(distribution, dim=-1)
        return samples, distribution

    def forward(self, x):
        type_scores = self.score(x)
        samples, distribution = self.sample(type_scores)
        return samples, distribution


class TypedBinaryTreeLSTMLayer(nn.Module):
    def __init__(self, hidden_value_dim, hidden_type_dim, type_predictor, binary_type_predictor,
                 max_seq_len, decoder_sem, decoder_init, decoder_sub, y_vocab, gumbel_temperature, dataset):
        super(TypedBinaryTreeLSTMLayer, self).__init__()
        self.hidden_value_dim = hidden_value_dim
        self.hidden_type_dim = hidden_type_dim
        # No need of functions for input types
        self.comp_linear_v = nn.ModuleList([nn.Linear(in_features=2 * (hidden_value_dim + hidden_type_dim),
                                                      out_features=5 * hidden_value_dim) for i in range(hidden_type_dim - 9)])
        self.type_predictor = type_predictor
        self.binary_type_predictor = binary_type_predictor
        self.max_seq_len = max_seq_len
        self.decoder_sem = decoder_sem
        self.decoder_init = decoder_init
        self.decoder_sub = decoder_sub
        self.y_vocab = y_vocab
        self.vocab_size = len(y_vocab)
        self.encoder = nn.GRU(self.vocab_size, self.hidden_value_dim, batch_first=True)
        self.gumbel_temperature = gumbel_temperature
        self.type_embedding = nn.Embedding(num_embeddings=self.hidden_type_dim,
                                           embedding_dim=self.hidden_value_dim)
        self.type_embedding.weight.requires_grad = False
        self.templates = nn.ParameterDict()
        self.dataset = dataset
        self.reset_parameters()

    def reset_gumbel_temp(self, new_temp):
        self.gumbel_temperature = new_temp

    def reduce_gumbel_temp(self, factor, iter, verbose=False):
        # TK DEBUG
        new_temp = np.maximum(self.gumbel_temperature * np.exp(-factor * iter), 0.5)
        if verbose:
            print(f'Gumbel temp lowered from {self.gumbel_temperature:g} to {new_temp:g}')
        self.gumbel_temperature = new_temp

    def reset_parameters(self):
        for i in range(self.hidden_type_dim - 9):
            init.orthogonal_(self.comp_linear_v[i].weight.data) # TODO: is this what we want?
            init.constant_(self.comp_linear_v[i].bias.data, val=0)

    def record_template(self, type, span):
        type_embedding = self.type_embedding(torch.tensor(type))
        self.templates[str(type)] = torch.nn.functional.gumbel_softmax(
                                        self.decoder_sem[span-2][type-9](type_embedding).view(8, span+1).log_softmax(-1),
                                        tau=1e-10, hard=True).detach()
        with open('output.txt', 'a') as file:
            file.write('template for type ' + str(type) + ' is: '
                        + str(self.templates[str(type)].argmax(-1)) + '\n')

    def apply_decoder_template(self, dt_sample, input_cat, spans):
        # dt_sample - B x K x N
        # input_cat - B x N x M x V
        # K is max template sequence length which will be smaller
        # than M the max decoding length

        B, N, M, V = input_cat.size()
        K = dt_sample.size(1)
        result = torch.zeros(B, M, V)

        pad_vector = int_to_one_hot(self.y_vocab.token_to_idx('<PAD>'), V)
        for i in range(B):
            # extract the arguments from the input
            idx = 0
            # extract the arguments from the input
            for t in range(K):  # template index
                # template_code = torch.zeros(1, 3)
                # template_code[:, dt_sample[i][k][t].argmax(-1)] = 1.0 # already sampled
                template_code = dt_sample[i, t, :spans[i]+1].unsqueeze(0).float()
                choices = [input_cat[i][n].flatten() for n in range(spans[i])]
                choices.insert(0, pad_vector.flatten())
                choices = torch.stack(choices, dim=0)
                output = torch.mm(template_code, choices).view(M, V)
                # copy non-trailing pad part of the output
                # DEBUG
                # output_len = torch.count_nonzero(output.sum(-1)).tolist()
                output_idx = output.argmax(-1)
                output_len = 0
                for j in range(M):
                    if output_idx[j] != 0:
                        output_len = j+1
                output_len = min(output_len, M-idx)
                result[i][idx:idx+output_len] = output[:output_len]
                idx = idx + output_len

        return result
    
    def apply_substitution_template(self, new_d, variables, temp_sub):
        B, M, _, V = new_d.size()
        y_vector = int_to_one_hot(self.y_vocab.token_to_idx('y'), V)
        y_var = torch.zeros(3, V)
        y_var[0] = y_vector
        for i in range(B):
            # get choices
            choices = [y_var.flatten()]
            for n in range(20):
                if torch.equal(variables[i, n], torch.zeros(3, V)):
                    choices.append(y_var.flatten())
                else:
                    choices.append(variables[i, n].flatten())
            choices = torch.stack(choices, dim=0)
            # get variable list ordered according to the substitution template
            var_sub = torch.zeros(6, 3, V)
            for k in range(6):
                var_sub[k] = torch.mm(temp_sub[i, k].unsqueeze(0), choices).view(3, V)
            # subsitute into slots
            idx = 0
            for j in range(M):
                for t in range(14):
                    if torch.equal(new_d[i, j, t], y_vector):
                        # determine if variable is x_i
                        if torch.equal(var_sub[idx, 0], int_to_one_hot(self.y_vocab.token_to_idx('x'), V)):
                            new_d[i, j, t+3:] = new_d[i, j, t+1:12].clone()
                            new_d[i, j, t:t+3] = var_sub[idx]
                        else:
                            new_d[i, j, t] = var_sub[idx, 0]
                        # break if we reach the end of variable list
                        if idx == 5:
                            break
                        idx += 1
                if idx == 5:
                    break
        return new_d

    def forward(self, decodings, variables, target_types, spans):
        """
        Args:
            decodings: input decodings of size B x N x M x V
            variables: input variables of size B x N x 30 x 3 x V
        Returns:
            new_d: output decoding of size B x M x V
            dt_sample: templates of size B x K x 3
        """

        type_embedding = self.type_embedding(target_types)

        if self.dataset == 'SCAN':
            B, N, M, V = decodings.size()
            # TK FIXME -- put these in the args somewhere K=8, template_vocab_size=3
            K = 8
            dt_sample = torch.zeros(B, K, N+1)
            # hard code start template
            start_template = torch.full((K, 1), 0).squeeze()  # 0 = PAD
            start_template[0] = 1
            start_template = F.one_hot(start_template, N+1)
            for i in range(B):
                s = spans[i]
                # hard code start template
                if target_types[i] == 20:
                    dt_sample[i] = start_template
                elif str(target_types[i].item()) in self.templates.keys():
                    dt_sample[i, :, :s+1] = self.templates[str(target_types[i].item())]
                else:
                    dt_sample[i, :, :s+1] = torch.nn.functional.gumbel_softmax(
                        self.decoder_sem[s-2][target_types[i]-9](type_embedding[i]).view(K, s+1).log_softmax(-1),
                        tau=self.gumbel_temperature, hard=True)
            new_d = self.apply_decoder_template(dt_sample, decodings, spans)
            new_v = torch.zeros(B, 20, 3, V)

        elif self.dataset == 'COGS':
            B, N, M, _, V = decodings.size()
            new_d = torch.zeros(B, M, 14, V)
            new_v = torch.zeros(B, 20, 3, V)
            zero_vector = torch.full((V, 1), 0.).squeeze()
            zero_var = torch.zeros(3, V)
            pad_vector = int_to_one_hot(self.y_vocab.token_to_idx('<PAD>'), V)
            # concatenate input decodings
            for i in range(B):
                target_type = target_types[i].item()
                if target_type in copy_temp_cogs.keys():
                    copy_temps = copy_temp_cogs[target_type]
                    if copy_temps == []: # remove!!
                        copy_temps = None
                else:
                    copy_temps = None
                choices = torch.zeros(M, 14, V)
                exp_idx = 0
                for j in range(N):
                    for t in range(M):
                        if exp_idx == M:
                            break
                        idx = 0
                        new_exp = False
                        while idx < 14 and not torch.equal(decodings[i, j, t, idx], pad_vector) and \
                            not torch.equal(decodings[i, j, t, idx], zero_vector):
                            new_exp = True
                            choices[exp_idx, idx] = decodings[i, j, t, idx]
                            idx += 1
                        if new_exp:
                            exp_idx += 1
                if copy_temps == None:
                    new_d[i] = choices
                else:
                    copy_temp = copy_temps[exp_idx]
                    for t in range(M):
                        if t == len(copy_temp):
                            break
                        new_d[i, t] = choices[copy_temp[t]]
            temp_sub = torch.zeros(B, 6, 21)
            for i in range(B):
                # concatenate input variables
                idx = 0
                for j in range(N):
                    for k in range(20):
                        if not torch.equal(variables[i, j, k], zero_var):
                            new_v[i, idx] = variables[i, j, k]
                            idx += 1
                # get substitution templates
                if target_types[i].item() != 87:
                    template = self.decoder_sub[target_types[i]](type_embedding[i]).view(6, 21)
                    temp_sub[i, :, :idx+1] = torch.nn.functional.gumbel_softmax(
                            template[:, :idx+1].log_softmax(-1), tau=self.gumbel_temperature, hard=True)
                    # debug
                    #print('template for type ' + str(target_types[i].item()) + ' is ' + str(temp_sub[i, :, :idx+1].argmax(-1)))
                else:
                    temp_sub[i, :, 0] = torch.ones(6)
            new_d = self.apply_substitution_template(new_d, new_v, temp_sub)
        return new_d, new_v


class TypedBinaryTreeLSTM(nn.Module):

    def __init__(self, word_dim, hidden_value_dim, hidden_type_dim, use_leaf_rnn, intra_attention,
                 gumbel_temperature, bidirectional, max_seq_len, decoder_sem, decoder_init, decoder_sub, x_vocab,
                 y_vocab, dataset, is_lstm=False, scan_token_to_type_map=None, input_tokens=None, positions_force=None,
                 types_force=None):
        super(TypedBinaryTreeLSTM, self).__init__()
        self.word_dim = word_dim
        self.hidden_value_dim = hidden_value_dim
        self.hidden_type_dim = hidden_type_dim
        self.use_leaf_rnn = use_leaf_rnn
        self.intra_attention = intra_attention
        self.gumbel_temperature = gumbel_temperature
        self.bidirectional = bidirectional
        self.is_lstm = is_lstm
        self.scan_token_to_type_map = scan_token_to_type_map
        self.input_tokens = input_tokens
        self.max_seq_len = max_seq_len
        self.decoder_sem = decoder_sem
        self.decoder_init = decoder_init
        self.decoder_sub = decoder_sub
        self.x_vocab = x_vocab
        self.y_vocab = y_vocab
        self.positions_force = positions_force
        self.types_force = types_force
        self.initial_decoder = torch.nn.Linear(hidden_value_dim, len(self.decoder_init.vocab))
        self.dataset = dataset

        assert not (self.bidirectional and not self.use_leaf_rnn)

        if use_leaf_rnn:
            self.leaf_rnn_cell = nn.LSTMCell(
                input_size=word_dim, hidden_size=hidden_value_dim)
            if bidirectional:
                self.leaf_rnn_cell_bw = nn.LSTMCell(
                    input_size=word_dim, hidden_size=hidden_value_dim)
        else:
            self.word_linear = nn.Linear(in_features=word_dim,
                                         out_features=2 * hidden_value_dim)
        self.type_predictor = TypePredictor(hidden_value_dim, [32, 32], hidden_type_dim)
        self.binary_type_predictor = TypePredictor(2 * hidden_type_dim, [32, 32], hidden_type_dim)

        if self.bidirectional:
            self.treelstm_layer = TypedBinaryTreeLSTMLayer(2 * hidden_value_dim, 2 * hidden_type_dim,
                                                           self.type_predictor,
                                                           self.binary_type_predictor,
                                                           self.max_seq_len,
                                                           self.decoder_sem,
                                                           self.decoder_init,
                                                           self.decoder_sub,
                                                           self.y_vocab,
                                                           self.gumbel_temperature,
                                                           self.dataset)
            self.comp_query = nn.Parameter(torch.FloatTensor(2 * (hidden_value_dim + hidden_type_dim)))
        else:
            self.treelstm_layer = TypedBinaryTreeLSTMLayer(hidden_value_dim, hidden_type_dim,
                                                           self.type_predictor,
                                                           self.binary_type_predictor,
                                                           self.max_seq_len,
                                                           self.decoder_sem,
                                                           self.decoder_init,
                                                           self.decoder_sub,
                                                           self.y_vocab,
                                                           self.gumbel_temperature,
                                                           self.dataset)
            self.comp_query = nn.Parameter(torch.FloatTensor(hidden_value_dim + hidden_type_dim))

        self.reset_parameters()

    def reduce_gumbel_temp(self, it):
        factor = 0.001
        self.gumbel_temperature = np.maximum(self.gumbel_temperature * np.exp(-factor * it), 0.5)
        self.type_predictor.reduce_gumbel_temp(factor, it, verbose=True)
        self.binary_type_predictor.reduce_gumbel_temp(factor, it, verbose=True)
        self.treelstm_layer.reduce_gumbel_temp(factor, it, verbose=True)

    def reset_gumbel_temp(self, new_temp):
        self.type_predictor.gumbel_temp = new_temp
        self.treelstm_layer.reset_gumbel_temp(new_temp)
        self.binary_type_predictor.gumbel_temp = new_temp
        self.treelstm_layer.gumbel_temp = new_temp

    def reset_parameters(self):
        if self.use_leaf_rnn:
            init.kaiming_normal_(self.leaf_rnn_cell.weight_ih.data)
            init.orthogonal_(self.leaf_rnn_cell.weight_hh.data)
            init.constant_(self.leaf_rnn_cell.bias_ih.data, val=0)
            init.constant_(self.leaf_rnn_cell.bias_hh.data, val=0)
            # Set forget bias to 1
            self.leaf_rnn_cell.bias_ih.data.chunk(4)[1].fill_(1)
            if self.bidirectional:
                init.kaiming_normal_(self.leaf_rnn_cell_bw.weight_ih.data)
                init.orthogonal_(self.leaf_rnn_cell_bw.weight_hh.data)
                init.constant_(self.leaf_rnn_cell_bw.bias_ih.data, val=0)
                init.constant_(self.leaf_rnn_cell_bw.bias_hh.data, val=0)
                # Set forget bias to 1
                self.leaf_rnn_cell_bw.bias_ih.data.chunk(4)[1].fill_(1)
        else:
            init.kaiming_normal_(self.word_linear.weight.data)
            init.constant_(self.word_linear.bias.data, val=0)
        self.treelstm_layer.reset_parameters()
        init.normal_(self.comp_query.data, mean=0, std=0.01)

    def get_initial_scan(self, initial_decodings, input, input_tokens):
        B, L, M, target_vocab_size = initial_decodings.size()
        initial_decodings[:, :, 0, :] = self.initial_decoder(input).softmax(-1)
        pad_decoding = int_to_one_hot(self.y_vocab.token_to_idx('<PAD>'), target_vocab_size)
        for i in range(B):
            for j in range(L):
                input_token = self.x_vocab.idx_to_token(input_tokens[i, j].item())
                if input_token in initial_decodings_scan.keys():
                    target_token = initial_decodings_scan[input_token]
                    initial_decodings[i, j, 0, :] = int_to_one_hot(self.y_vocab.token_to_idx(target_token), target_vocab_size)
                else:
                    initial_decodings[i, j, 0, :] = pad_decoding
        return initial_decodings
    
    def get_initial_cogs(self, initial_decodings, initial_variables, input, input_tokens):
        B, L, M, _, V = initial_decodings.size()
        #initial_decodings[:, :, 0, :] = self.initial_decoder(input).softmax(-1)
        for i in range(B):
            for j in range(L):
                input_token = self.x_vocab.idx_to_token(input_tokens[i, j].item())
                if input_token != '<PAD>':
                    target_tokens = initial_decodings_cogs[input_token]
                    # get initial variables
                    #initial_variables[i, j, 0, 0, :] = int_to_one_hot(self.y_vocab.token_to_idx('x'), V)
                    #initial_variables[i, j, 0, 1, :] = int_to_one_hot(self.y_vocab.token_to_idx('_'), V)
                    #initial_variables[i, j, 0, 2, :] = int_to_one_hot(self.y_vocab.token_to_idx(str(j)), V)
                    # remove x _s
                    initial_variables[i, j, 0, 0, :] = int_to_one_hot(self.y_vocab.token_to_idx(str(j)), V)
                    if input_token in initial_variables_cogs.keys():
                        target_variable = initial_variables_cogs[input_token]
                        initial_variables[i, j, 1, 0, :] = int_to_one_hot(self.y_vocab.token_to_idx(target_variable), V)
                    # get initial decodings
                    target_tokens = [[token for token in tokens.split(' ') if token != ''] for tokens in target_tokens.split('|')]
                    for k in range(len(target_tokens)):
                        for t in range(len(target_tokens[k])):
                            initial_decodings[i, j, k, t] = int_to_one_hot(self.y_vocab.token_to_idx(target_tokens[k][t]), V)
        return initial_decodings, initial_variables

    def forward(self, input, length, input_tokens, positions_force=None, types_force=None, spans_force=None):
        max_depth = input.size(1)
        # decode each word separately ((B*L), max_seq_len, target vocab size)
        # reshape to B x L x max_seq_len x len(decoder.vocab)
        target_vocab_size = len(self.decoder_init.vocab)
        B, L, _ = input.size()
        if self.scan_token_to_type_map is not None:
            target_types = self.scan_token_to_type_map[input_tokens.view(B*L)]
        M = int(self.max_seq_len / 14)
        initial_variables = torch.zeros(B, L, 20, 3, target_vocab_size)
        if self.dataset == 'SCAN':
            initial_decodings = torch.zeros(B, L, 10, self.max_seq_len, target_vocab_size)
            initial_decodings = self.get_initial_scan(initial_decodings, input, input_tokens)
        elif self.dataset == 'COGS':
            initial_decodings = torch.zeros(B, L, M, 14, target_vocab_size)
            initial_decodings[:, :, :, :, self.y_vocab.token_to_idx('<PAD>')] = 1
            initial_decodings, initial_variables = self.get_initial_cogs(initial_decodings, initial_variables, input, input_tokens)
        decodings = initial_decodings
        variables = initial_variables
        for t in range(max_depth - 1):
            if types_force != None:
                # get target types for this step and remove them
                if self.dataset == 'SCAN':
                    target_types = torch.tensor([20 for _ in range(B)]) # default to start
                elif self.dataset == 'COGS':
                    target_types = torch.tensor([87 for _ in range(B)])
                for k in range(len(types_force)):
                    if types_force[k] != []:
                        target_types[k] = types_force[k][-1]
                        types_force[k].pop(-1)
            else:
                target_types = None
            positions = torch.zeros(B).long()
            spans = torch.zeros(B).long()
            for i in range(B):
                if positions_force[i] == []:
                    positions[i] = 0 # default to start
                else:
                    positions[i] = positions_force[i].pop(-1)
                if spans_force[i] == []:
                    spans[i] = 2 # default to binary rules
                else:
                    spans[i] = spans_force[i].pop(-1)
            N = max(spans) # find max span length
            V = len(self.y_vocab)
            input_decodings = torch.zeros(B, N, M, 14, V)
            input_variables = torch.zeros(B, N, 20, 3, V)
            for i in range(B):
                input_decodings[i, :spans[i]] = decodings[i, positions[i]:positions[i]+spans[i]].view(spans[i].item(), M, 14, V)
                input_variables[i, :spans[i]] = variables[i, positions[i]:positions[i]+spans[i]]
            output_decodings, output_variables = self.treelstm_layer(input_decodings, input_variables, target_types, spans)
            _, L, _, _, _ = decodings.size()
            new_d = torch.zeros(B, L, M, 14, V)
            new_v = torch.zeros(B, L, 20, 3, V)
            for i in range(B):
                s = spans[i]
                p = positions[i].unsqueeze(0)
                new_d[i, :p] = decodings[i, :p]
                new_d[i, p] = output_decodings[i]
                new_d[i, p+1:L-s+1] = decodings[i, p+s:L]
                new_v[i, :p] = variables[i, :p]
                new_v[i, p] = output_variables[i]
                new_v[i, p+1:L-s+1] = variables[i, p+s:L]
            decodings = new_d
            variables = new_v
        # final normalization step
        decodings = decodings[:, 0].squeeze(1).flatten(start_dim=1, end_dim=2)
        # ignore paddings
        new_d = torch.zeros(B, M * 14, V)
        pad_decoding = int_to_one_hot(self.y_vocab.token_to_idx('<PAD>'), V)
        zero_vector = torch.zeros(V)
        for i in range(B):
            idx = 0
            # copy expressions that start with * first
            for k in range(M):
                new_exp = False
                if torch.equal(decodings[i, k * 14], int_to_one_hot(self.y_vocab.token_to_idx('*'), V)):
                    for j in range(14):
                        if not torch.equal(decodings[i, k * 14 + j], pad_decoding) and \
                           not torch.equal(decodings[i, k * 14 + j], zero_vector):
                            new_exp = True
                            new_d[i, idx] = decodings[i, k * 14 + j]
                            idx += 1
                    if new_exp:
                        new_d[i, idx] = int_to_one_hot(self.y_vocab.token_to_idx(';'), V)
                        idx += 1
            last_idx = 0
            for k in range(M):
                if torch.equal(decodings[i, k * 14], int_to_one_hot(self.y_vocab.token_to_idx('*'), V)):
                    continue # skip expressions that start with *
                new_exp = False
                for j in range(14):
                    if not torch.equal(decodings[i, k * 14 + j], pad_decoding) and \
                       not torch.equal(decodings[i, k * 14 + j], zero_vector):
                        new_exp = True
                        new_d[i, idx] = decodings[i, k * 14 + j]
                        idx += 1
                if new_exp:
                    new_d[i, idx] = int_to_one_hot(self.y_vocab.token_to_idx('and'), V)
                    last_idx = idx
                    idx += 1
            new_d[i, last_idx] = pad_decoding
        return new_d