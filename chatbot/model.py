"""
Model to predict the next sentence given an input sequence

"""

import tensorflow as tf
import tensorflow_addons as tfa
from chatbot.textdata import Batch


class ProjectionOp:
    """ Single layer perceptron
    Project input tensor on the output dimension
    """

    def __init__(self, shape, scope=None, dtype=None):
        """
        Args:
            shape: a tuple (input dim, output dim)
            scope (str): encapsulate variables
            dtype: the weights type
        """
        assert len(shape) == 2

        self.scope = scope

        # Projection on the keyboard
        with tf.compat.v1.variable_scope('weights_' + self.scope):
            self.W_t = tf.compat.v1.get_variable(
                'weights',
                shape,
                # initializer=tf.truncated_normal_initializer()  # TODO: Tune value (fct of input size: 1/sqrt(
                #  input_dim))
                dtype=dtype)
            self.b = tf.compat.v1.get_variable(
                'bias',
                shape[0],
                initializer=tf.compat.v1.constant_initializer(),
                dtype=dtype)
            self.W = tf.transpose(a=self.W_t)

    def get_weights(self):
        """ Convenience method for some tf arguments
        """
        return self.W, self.b

    def __call__(self, x):
        """ Project the output of the decoder into the vocabulary space
        Args:
            x (tf.Tensor): input value
        """
        with tf.compat.v1.name_scope(self.scope):
            return tf.matmul(x, self.W) + self.b


class Model:
    """
    Implementation of a seq2seq model.
    Architecture:
        Encoder/decoder
        2 LTSM layers
    """

    def __init__(self, args, text_data):
        """
        Args:
            args: parameters of the model
            text_data: the dataset object
        """
        print("Model creation...")

        self.textData = text_data  # Keep a reference on the dataset
        self.args = args  # Keep track of the parameters of the model
        self.dtype = tf.float32

        # Placeholders
        self.encoderInputs = None
        self.decoderInputs = None  # Same that decoderTarget plus the <go>
        self.decoderTargets = None
        self.decoderWeights = None  # Adjust the learning to the target sentence size

        # Main operators
        self.lossFct = None
        self.optOp = None
        self.outputs = None  # Outputs of the network, list of probability for each words

        # Construct the graphs
        self.build_network()

    def build_network(self):
        """ Create the computational graph
        """

        # TODO: Create name_scopes (for better graph visualisation)
        # TODO: Use buckets (better perfs)

        # Parameters of sampled softmax (needed for attention mechanism and a large vocabulary size)
        output_projection = None
        # Sampled softmax only makes sense if we sample less than vocabulary size.
        if 0 < self.args.softmaxSamples < self.textData.getVocabularySize():
            output_projection = ProjectionOp(
                (self.textData.getVocabularySize(), self.args.hiddenSize),
                scope='softmax_projection',
                dtype=self.dtype
            )

            def sampled_softmax(labels, inputs):
                labels = tf.reshape(labels, [-1, 1])  # Add one dimension (nb of true classes, here 1)

                # We need to compute the sampled_softmax_loss using 32bit floats to
                # avoid numerical instabilities.
                local_wt = tf.cast(output_projection.W_t, tf.float32)
                local_b = tf.cast(output_projection.b, tf.float32)
                local_inputs = tf.cast(inputs, tf.float32)

                return tf.cast(
                    tf.nn.sampled_softmax_loss(
                        local_wt,  # Should have shape [num_classes, dim]
                        local_b,
                        labels,
                        local_inputs,
                        self.args.softmaxSamples,  # The number of classes to randomly sample per batch
                        self.textData.getVocabularySize()),  # The number of classes
                    self.dtype)

        # Creation of the rnn cell
        def create_rnn_cell():
            edc = tf.keras.layers.LSTMCell(  # Or GRUCell, LSTMCell(args.hiddenSize)
                self.args.hiddenSize,
            )
            if not self.args.test:  # TODO: Should use a placeholder instead
                edc = tf.compat.v1.nn.rnn_cell.DropoutWrapper(
                    edc,
                    input_keep_prob=1.0,
                    output_keep_prob=self.args.dropout
                )
            return edc

        enco_deco_cell = tf.keras.layers.StackedRNNCells(
            [create_rnn_cell() for _ in range(self.args.numLayers)],
        )

        # Network input (placeholders)

        with tf.compat.v1.name_scope('placeholder_encoder'):
            self.encoderInputs = [tf.compat.v1.placeholder(tf.int32, [None, ]) for _ in
                                  range(self.args.maxLengthEnco)]  # Batch size * sequence length * input dim

        with tf.compat.v1.name_scope('placeholder_decoder'):
            self.decoderInputs = [tf.compat.v1.placeholder(tf.int32, [None, ], name='inputs') for _ in
                                  range(self.args.maxLengthDeco)]  # Same sentence length for input and output (Right ?)
            self.decoderTargets = [tf.compat.v1.placeholder(tf.int32, [None, ], name='targets') for _ in
                                   range(self.args.maxLengthDeco)]
            self.decoderWeights = [tf.compat.v1.placeholder(tf.float32, [None, ], name='weights') for _ in
                                   range(self.args.maxLengthDeco)]

        # Define the network
        # Here we use an embedding model, it takes integer as input and convert them into word vector for
        # better word representation

        decoder_outputs, states = tf.contrib.legacy_seq2seq.embedding_rnn_seq2seq(
            self.encoderInputs,  # List<[batch=?, inputDim=1]>, list of size args.maxLength
            self.decoderInputs,  # For training, we force the correct output (feed_previous=False)
            enco_deco_cell,
            self.textData.getVocabularySize(),
            self.textData.getVocabularySize(),  # Both encoder and decoder have the same number of class
            embedding_size=self.args.embeddingSize,  # Dimension of each word
            output_projection=output_projection.get_weights() if output_projection else None,
            feed_previous=bool(self.args.test)
            # When we test (self.args.test), we use previous output as next input (feed_previous)
        )

        # TODO: When the LSTM hidden size is too big, we should project the LSTM output into a smaller space (4086 =>
        #  2046): Should speed up training and reduce memory usage. Other solution, use sampling softmax

        # For testing only
        if self.args.test:
            if not output_projection:
                self.outputs = decoder_outputs
            else:
                self.outputs = [output_projection(output) for output in decoder_outputs]

            # TODO: Attach a summary to visualize the output

        # For training only
        else:
            # Finally, we define the loss function
            self.lossFct = tf.contrib.legacy_seq2seq.sequence_loss(
                decoder_outputs,
                self.decoderTargets,
                self.decoderWeights,
                self.textData.getVocabularySize(),
                softmax_loss_function=sampled_softmax if output_projection else None  # If None, use default SoftMax
            )
            tf.compat.v1.summary.scalar('loss', self.lossFct)  # Keep track of the cost

            # Initialize the optimizer
            opt = tf.compat.v1.train.AdamOptimizer(
                learning_rate=self.args.learningRate,
                beta1=0.9,
                beta2=0.999,
                epsilon=1e-08
            )
            self.optOp = opt.minimize(self.lossFct)

    def step(self, batch):
        """ Forward/training step operation. Does not perform run on itself but just return the operators to do so.
        Those have then to be run Args: batch (Batch): Input data on testing mode, input and target on output mode
        Return: (ops), dict: A tuple of the (training, loss) operators or (outputs,) in testing mode with the
        associated feed dictionary
        """

        # Feed the dictionary
        feed_dict = {}
        ops = None

        if not self.args.test:  # Training
            for i in range(self.args.maxLengthEnco):
                feed_dict[self.encoderInputs[i]] = batch.encoderSeqs[i]
            for i in range(self.args.maxLengthDeco):
                feed_dict[self.decoderInputs[i]] = batch.decoderSeqs[i]
                feed_dict[self.decoderTargets[i]] = batch.targetSeqs[i]
                feed_dict[self.decoderWeights[i]] = batch.weights[i]

            ops = (self.optOp, self.lossFct)
        else:  # Testing (batchSize == 1)
            for i in range(self.args.maxLengthEnco):
                feed_dict[self.encoderInputs[i]] = batch.encoderSeqs[i]
            feed_dict[self.decoderInputs[0]] = [self.textData.goToken]

            ops = (self.outputs,)

        # Return one pass operator
        return ops, feed_dict
