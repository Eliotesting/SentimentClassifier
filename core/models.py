import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
import torch.nn.functional as F

import math

import logging
logger = logging.getLogger(__name__)

def get_optimizer(args, params):
    """
    Get the optimizer class from PyTorch depending on the argument specified
    """
    
    import torch.optim as optim
    if args.algorithm == 'rmsprop':
        optimizer = optim.RMSprop(params, lr=args.learning_rate)
        # optimizer = optim.RMSprop(params, lr=args.learning_rate, alpha=0.9, eps=1e-06, weight_decay=0, momentum=0, centered=False)
    elif args.algorithm == 'adam':
        optimizer = optim.Adam(params, lr=args.learning_rate)
    return optimizer

class Attention(nn.Module):
    """Attention layer - Custom layer to perform weighted average over the second axis (axis=1)
        Transforming a tensor of size [N, W, H] to [N, 1, H].
        N: batch size
        W: number of words, different sentence length will need to be padded to have the same size for each mini-batch
        H: hidden state dimension or word embedding dimension
    Args:
        dim: The dimension of the word embedding
    Attributes:
        w: learnable weight matrix of size [dim, dim]
        v: learnable weight vector of size [dim]
    Examples::
        >>> m = models_pytorch.Attention(300)
        >>> input = Variable(torch.randn(4, 128, 300))
        >>> output = m(input)
        >>> print(output.size())
    """

    def __init__(self, dim):
        super(Attention, self).__init__()
        self.dim = dim
        self.att_weights = None
        self.w = nn.Parameter(torch.Tensor(dim, dim))
        self.v = nn.Parameter(torch.Tensor(dim))
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.w.size(1))
        self.w.data.uniform_(-stdv, stdv)
        self.v.data.uniform_(-stdv, stdv)

    def forward(self, dense_sentence):
        wplus = dense_sentence.matmul(self.w)
        wplus = torch.tanh(wplus)

        att_w = wplus.matmul(self.v)
        att_w = F.softmax(att_w,dim=1)

        # Save attention weights to be retrieved for visualization
        self.att_weights = att_w

        after_attention = torch.bmm(att_w.unsqueeze(1), dense_sentence)

        return after_attention

    def __repr__(self):
        return self.__class__.__name__ + ' (' \
            + '1' + ', ' \
            + str(self.dim) + ')'


#######################################################################################
## Static Helper Functions
#

def get_pretrained_embedding(lookup_table, vocab, emb_reader):
    """
    Method to initialize lookup table using a pre-trained embedding
    """
    logger.info('Initializing lookup table...')
    initialized_weight = emb_reader.get_emb_matrix_given_vocab(vocab, lookup_table.weight.data.tolist())
    logger.info('Initializing lookup table completed!')
    return torch.FloatTensor(initialized_weight)


def log(logString, tensor):
    logger.info(logString + str(tensor.size()))

def tensorLogger(method, result_tensor):
    """
    :param method:
        The method or the layer name to be printed with the tensor
    :param result_tensor:
        The result tensor to be printed
    :return:
        Returns the same result_tensor
    """

    # result_tensor = method(tensor)
    layer_name = ""
    if isinstance(method, str):
        layer_name = method
    else:
        layer_name = method.__class__.__name__
    
    log('%15s :' % layer_name, result_tensor)

def lstmWrapper(methodLstm, inputLstm):
    """
    A wrapper for LSTM
    """
    recc, (hn, cn) = methodLstm(inputLstm)
    return recc

def convWrapper(methodConv, inputConv):
    """
    A wrapper for Convplution layer because of the need to manipulate the dimension of input and output tensor
    Using convolution 2D requires some squeezing and unsqueezing
    """
    conv = inputConv.unsqueeze(1) # unsqueeze
    conv = methodConv(conv) # Apply the given CNN method
    conv = conv.squeeze() # squeeze
    conv = conv.permute(0, 2, 1) # permute(0,2,1)
    return conv

class ListModule(object):
    """
    A class to contain nn.Module inside nn.Module
    In this case it is used to store multiple cnn and rnn layers
    """
    def __init__(self, module, prefix, *args):
        self.module = module
        self.prefix = prefix
        self.num_module = 0
        for new_module in args:
            self.append(new_module)

    def append(self, new_module):
        if not isinstance(new_module, nn.Module):
            raise ValueError('Not a Module')
        else:
            self.module.add_module(self.prefix + str(self.num_module), new_module)
            self.num_module += 1

    def __len__(self):
        return self.num_module

    def __getitem__(self, i):
        if i < 0 or i >= self.num_module:
            raise IndexError('Out of bound')
        return getattr(self.module, self.prefix + str(i))

class GenericNN(nn.Module):
    def __init__(self, args, vocab, emb_reader):
        """
        :param args:
            the arguments from the main function containing all the configuration options
        :param vocab:
            vocabulary mapping from word to indices to initialize pre-trained word embeddings
        :param emb_reader:
            Embedding reader class which handles initialization of pre-trained word embeddings
        """
        super(GenericNN, self).__init__()
        self.model_type = args.model_type
        self.pooling_type = args.pooling_type
        self.dropout_rate = args.dropout_rate
        self.att_weights = None # Attribute to save attention weights

        self.lookup_table = nn.Embedding(args.vocab_size, args.emb_dim)
        if emb_reader:
            self.lookup_table.weight.data = get_pretrained_embedding(self.lookup_table, vocab, emb_reader)
        
        self.cnn = None
        if "c" in self.model_type:
            self.cnn = ListModule(self, 'cnn_')
            for i in range(args.cnn_layer):
                self.cnn.append(nn.Conv2d(in_channels=1,
                            out_channels=args.cnn_dim,
                            kernel_size=(args.cnn_window_size, args.emb_dim),
                            padding=(args.cnn_window_size//2, 0)) # padding is on both sides, so padding=1 means it adds 1 on the left and 1 on the right
                )
        
        if "r" in self.model_type:
            self.rnn = ListModule(self, 'rnn_')
            for i in range(args.rnn_layer):
                rnn_dropout = self.dropout_rate
                if (i == args.rnn_layer - 1): # If final layer, make dropout 0, Only apply dropout on the first n-1 layers
                    rnn_dropout = 0
                if ("b" in args.model_type): # If bidirectional RNN
                    self.rnn.append(nn.LSTM(args.cnn_dim, args.rnn_dim//2, batch_first=True, dropout=rnn_dropout, bidirectional=True))
                else:
                    self.rnn.append(nn.LSTM(args.cnn_dim, args.rnn_dim, batch_first=True, dropout=rnn_dropout))

        self.attention = None
        if self.pooling_type == 'att':
            self.attention = Attention(args.rnn_dim)

        self.linear = nn.Linear(args.rnn_dim, 1)

    def forward(self, sentence, training=False):
        """
        :param sentence:
                input sentence is in size of [N, W]
                N: batch size
                W: number of words, different sentence length will need to be padded to have the same size for each mini-batch
        :param training:
                boolean value, whether the forward is for training purpose
        :param batch_number:
                The current batch number
        :return:
                a tensor [C], where C is the number of classes
        """

        pass



class CRNN(GenericNN):
    def __init__(self, args, vocab, emb_reader):
        super(CRNN, self).__init__(args, vocab, emb_reader)
     
    def forward(self, sentence, training=False):
        embed    = self.lookup_table(sentence)
        conv     = embed
        
        if "c" in self.model_type:
            for curr_cnn in self.cnn:
                prevConv = conv
                conv     = convWrapper(curr_cnn, conv)
                conv     = F.dropout(conv, p=self.dropout_rate, training=training)
            
        recc     = conv

        if "r" in self.model_type:
            for curr_rnn in self.rnn:
                prevRecc = recc
                recc     = lstmWrapper(curr_rnn, recc)
                recc     = F.dropout(recc, p=self.dropout_rate, training=training)

        if self.model_type == 'crcrnn':
            assert (len(self.cnn) == len(self.rnn))
            for i in range(len(self.cnn)):
                prevConv = conv
                conv     = convWrapper(self.cnn[i], conv)
                conv     = F.dropout(conv, p=self.dropout_rate, training=training)
                conv     = lstmWrapper(self.rnn[i], conv)
                conv     = F.dropout(conv, p=self.dropout_rate, training=training)
            recc = conv

        if "att" in self.pooling_type:
            pool      = self.attention(recc)
            self.att_weights = self.attention.att_weights # Save attention weights
        else:
            pool      = F.avg_pool2d(recc, (recc.size()[1],1))
        pool      = pool.squeeze(1)

        outlinear = self.linear(pool)
        pred_prob = torch.sigmoid(outlinear)
        pred_prob = pred_prob.squeeze()
        return pred_prob
