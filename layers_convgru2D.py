import numpy as np
import tensorflow as tf
tf.keras.backend.set_floatx('float16')

from tensorflow.python.ops import inplace_ops

##New imports
from tensorflow.keras import activations
from tensorflow.keras import backend as K
from tensorflow.keras import constraints
from tensorflow.keras import initializers
from tensorflow.python.framework import tensor_shape
from tensorflow.keras import regularizers

from tensorflow.keras.layers import Layer
from tensorflow.python.keras.layers.recurrent import _standardize_args, DropoutRNNCellMixin, RNN, _is_multiple_state
from tensorflow.python.keras.utils import conv_utils, generic_utils, tf_utils
from tensorflow.python.keras.engine.input_spec import InputSpec
from tensorflow.python.ops import array_ops
from tensorflow.python.util import nest
from tensorflow.python.util.tf_export import keras_export

from tensorflow.keras.layers import Conv2D, RNN
from layers_attn import MultiHead2DAttention_v2, _generate_relative_positions_embeddings, _relative_attention_inner, attn_shape_adjust


class ConvRNN2D(RNN):
  """Base class for convolutional-recurrent layers.
    
    #This calss has been adated to work with mixed precision training
    
    Arguments:
      cell: A RNN cell instance. A RNN cell is a class that has:
        - a `call(input_at_t, states_at_t)` method, returning
          `(output_at_t, states_at_t_plus_1)`. The call method of the
          cell can also take the optional argument `constants`, see
          section "Note on passing external constants" below.
        - a `state_size` attribute. This can be a single integer
          (single state) in which case it is
          the number of channels of the recurrent state
          (which should be the same as the number of channels of the cell
          output). This can also be a list/tuple of integers
          (one size per state). In this case, the first entry
          (`state_size[0]`) should be the same as
          the size of the cell output.
      return_sequences: Boolean. Whether to return the last output.
        in the output sequence, or the full sequence.
      return_state: Boolean. Whether to return the last state
        in addition to the output.
      go_backwards: Boolean (default False).
        If True, process the input sequence backwards and return the
        reversed sequence.
      stateful: Boolean (default False). If True, the last state
        for each sample at index i in a batch will be used as initial
        state for the sample of index i in the following batch.
      input_shape: Use this argument to specify the shape of the
        input when this layer is the first one in a model.

    Call arguments:
      inputs: A 5D tensor.
      mask: Binary tensor of shape `(samples, timesteps)` indicating whether
        a given timestep should be masked.
      training: Python boolean indicating whether the layer should behave in
        training mode or in inference mode. This argument is passed to the cell
        when calling it. This is for use with cells that use dropout.
      initial_state: List of initial state tensors to be passed to the first
        call of the cell.
      constants: List of constant tensors to be passed to the cell at each
        timestep.

    Input shape:
      5D tensor with shape:
      `(samples, timesteps, channels, rows, cols)`
      if data_format='channels_first' or 5D tensor with shape:
      `(samples, timesteps, rows, cols, channels)`
      if data_format='channels_last'.

    Output shape:
      - If `return_state`: a list of tensors. The first tensor is
        the output. The remaining tensors are the last states,
        each 4D tensor with shape:
        `(samples, filters, new_rows, new_cols)`
        if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, filters)`
        if data_format='channels_last'.
        `rows` and `cols` values might have changed due to padding.
      - If `return_sequences`: 5D tensor with shape:
        `(samples, timesteps, filters, new_rows, new_cols)`
        if data_format='channels_first'
        or 5D tensor with shape:
        `(samples, timesteps, new_rows, new_cols, filters)`
        if data_format='channels_last'.
      - Else, 4D tensor with shape:
        `(samples, filters, new_rows, new_cols)`
        if data_format='channels_first'
        or 4D tensor with shape:
        `(samples, new_rows, new_cols, filters)`
        if data_format='channels_last'.

    Masking:
      This layer supports masking for input data with a variable number
      of timesteps.

    Note on using statefulness in RNNs:
      You can set RNN layers to be 'stateful', which means that the states
      computed for the samples in one batch will be reused as initial states
      for the samples in the next batch. This assumes a one-to-one mapping
      between samples in different successive batches.
      To enable statefulness:
        - Specify `stateful=True` in the layer constructor.
        - Specify a fixed batch size for your model, by passing
          - If sequential model:
              `batch_input_shape=(...)` to the first layer in your model.
          - If functional model with 1 or more Input layers:
              `batch_shape=(...)` to all the first layers in your model.
              This is the expected shape of your inputs
              *including the batch size*.
              It should be a tuple of integers,
              e.g. `(32, 10, 100, 100, 32)`.
              Note that the number of rows and columns should be specified
              too.
        - Specify `shuffle=False` when calling fit().
      To reset the states of your model, call `.reset_states()` on either
      a specific layer, or on your entire model.

    Note on specifying the initial state of RNNs:
      You can specify the initial state of RNN layers symbolically by
      calling them with the keyword argument `initial_state`. The value of
      `initial_state` should be a tensor or list of tensors representing
      the initial state of the RNN layer.
      You can specify the initial state of RNN layers numerically by
      calling `reset_states` with the keyword argument `states`. The value of
      `states` should be a numpy array or list of numpy arrays representing
      the initial state of the RNN layer.

    Note on passing external constants to RNNs:
      You can pass "external" constants to the cell using the `constants`
      keyword argument of `RNN.__call__` (as well as `RNN.call`) method. This
      requires that the `cell.call` method accepts the same keyword argument
      `constants`. Such constants can be used to condition the cell
      transformation on additional static inputs (not changing over time),
      a.k.a. an attention mechanism.
  """

  def __init__(self,
               cell,
               return_sequences=False,
               return_state=False,
               go_backwards=False,
               stateful=False,
               unroll=False,
               **kwargs):
    if unroll:
      raise TypeError('Unrolling isn\'t possible with '
                      'convolutional RNNs.')
    if isinstance(cell, (list, tuple)):
      # The StackedConvRNN2DCells isn't implemented yet.
      raise TypeError('It is not possible at the moment to'
                      'stack convolutional cells.')
    super(ConvRNN2D, self).__init__(cell,
                                    return_sequences,
                                    return_state,
                                    go_backwards,
                                    stateful,
                                    unroll,
                                    **kwargs)
    self.input_spec = [InputSpec(ndim=5)]
    self.states = None
    self._num_constants = None

  @tf_utils.shape_type_conversion
  def compute_output_shape(self, input_shape):
    if isinstance(input_shape, list):
      input_shape = input_shape[0]

    cell = self.cell
    if cell.data_format == 'channels_first':
      rows = input_shape[3]
      cols = input_shape[4]
    elif cell.data_format == 'channels_last':
      rows = input_shape[2]
      cols = input_shape[3]
    rows = conv_utils.conv_output_length(rows,
                                         cell.kernel_size[0],
                                         padding=cell.padding,
                                         stride=cell.strides[0],
                                         dilation=cell.dilation_rate[0])
    cols = conv_utils.conv_output_length(cols,
                                         cell.kernel_size[1],
                                         padding=cell.padding,
                                         stride=cell.strides[1],
                                         dilation=cell.dilation_rate[1])

    if cell.data_format == 'channels_first':
      output_shape = input_shape[:2] + (cell.filters, rows, cols)
    elif cell.data_format == 'channels_last':
      output_shape = input_shape[:2] + (rows, cols, cell.filters)

    if not self.return_sequences:
      output_shape = output_shape[:1] + output_shape[2:]

    if self.return_state:
      output_shape = [output_shape]
      if cell.data_format == 'channels_first':
        output_shape += [(input_shape[0], cell.filters, rows, cols)
                         for _ in range(2)]
      elif cell.data_format == 'channels_last':
        output_shape += [(input_shape[0], rows, cols, cell.filters)
                         for _ in range(2)]
    return output_shape

  @tf_utils.shape_type_conversion
  def build(self, input_shape):
    # Note input_shape will be list of shapes of initial states and
    # constants if these are passed in __call__.
    if self._num_constants is not None:
      constants_shape = input_shape[-self._num_constants:]  # pylint: disable=E1130
    else:
      constants_shape = None

    if isinstance(input_shape, list):
      input_shape = input_shape[0]

    batch_size = input_shape[0] if self.stateful else None
    self.input_spec[0] = InputSpec(shape=(batch_size, None) + input_shape[2:5])

    # allow cell (if layer) to build before we set or validate state_spec
    if isinstance(self.cell, Layer):
      step_input_shape = (input_shape[0],) + input_shape[2:]
      if constants_shape is not None:
        self.cell.build([step_input_shape] + constants_shape)
      else:
        self.cell.build(step_input_shape)

    # set or validate state_spec
    if hasattr(self.cell.state_size, '__len__'):
      state_size = list(self.cell.state_size)
    else:
      state_size = [self.cell.state_size]

    if self.state_spec is not None:
      # initial_state was passed in call, check compatibility
      if self.cell.data_format == 'channels_first':
        ch_dim = 1
      elif self.cell.data_format == 'channels_last':
        ch_dim = 3
      if [spec.shape[ch_dim] for spec in self.state_spec] != state_size:
        raise ValueError(
            'An initial_state was passed that is not compatible with '
            '`cell.state_size`. Received `state_spec`={}; '
            'However `cell.state_size` is '
            '{}'.format([spec.shape for spec in self.state_spec],
                        self.cell.state_size))
    else:
      if self.cell.data_format == 'channels_first':
        self.state_spec = [InputSpec(shape=(None, dim, None, None))
                           for dim in state_size]
      elif self.cell.data_format == 'channels_last':
        self.state_spec = [InputSpec(shape=(None, None, None, dim))
                           for dim in state_size]
    if self.stateful:
      self.reset_states()
    self.built = True

  def get_initial_state(self, inputs):
    # (samples, timesteps, rows, cols, filters)
    initial_state = K.zeros_like(inputs)
    # (samples, rows, cols, filters)
    initial_state = K.sum(initial_state, axis=1)
    shape = list(self.cell.kernel_shape)
    shape[-1] = self.cell.filters
    initial_state = self.cell.input_conv(initial_state,
                                         tf.cast( array_ops.zeros(tuple(shape)), dtype=self._compute_dtype),
                                         padding=self.cell.padding)

    if hasattr(self.cell.state_size, '__len__'):
      return [initial_state for _ in self.cell.state_size]
    else:
      return [initial_state]

  def __call__(self, inputs, initial_state=None, constants=None, **kwargs):
    inputs, initial_state, constants = _standardize_args(
        inputs, initial_state, constants, self._num_constants)

    if initial_state is None and constants is None:
      return super(ConvRNN2D, self).__call__(inputs, **kwargs)

    # If any of `initial_state` or `constants` are specified and are Keras
    # tensors, then add them to the inputs and temporarily modify the
    # input_spec to include them.

    additional_inputs = []
    additional_specs = []
    if initial_state is not None:
      kwargs['initial_state'] = initial_state
      additional_inputs += initial_state
      self.state_spec = []
      for state in initial_state:
        shape = K.int_shape(state)
        self.state_spec.append(InputSpec(shape=shape))

      additional_specs += self.state_spec
    if constants is not None:
      kwargs['constants'] = constants
      additional_inputs += constants
      self.constants_spec = [InputSpec(shape=K.int_shape(constant))
                             for constant in constants]
      self._num_constants = len(constants)
      additional_specs += self.constants_spec
    # at this point additional_inputs cannot be empty
    for tensor in additional_inputs:
      if K.is_keras_tensor(tensor) != K.is_keras_tensor(additional_inputs[0]):
        raise ValueError('The initial state or constants of an RNN'
                         ' layer cannot be specified with a mix of'
                         ' Keras tensors and non-Keras tensors')

    if K.is_keras_tensor(additional_inputs[0]):
      # Compute the full input spec, including state and constants
      full_input = [inputs] + additional_inputs
      full_input_spec = self.input_spec + additional_specs
      # Perform the call with temporarily replaced input_spec
      original_input_spec = self.input_spec
      self.input_spec = full_input_spec
      output = super(ConvRNN2D, self).__call__(full_input, **kwargs)
      self.input_spec = original_input_spec
      return output
    else:
      return super(ConvRNN2D, self).__call__(inputs, **kwargs)

  def call(
        self,
            inputs,
            mask=None,
            training=None,
            initial_state=None,
            constants=None):
    # note that the .build() method of subclasses MUST define
    # self.input_spec and self.state_spec with complete input shapes.
    if isinstance(inputs, list):
      inputs = inputs[0]
    if initial_state is not None:
      pass
    elif self.stateful:
      initial_state = self.states
    else:
      initial_state = self.get_initial_state(inputs)

    if isinstance(mask, list):
      mask = mask[0]

    if len(initial_state) != len(self.states):
      raise ValueError('Layer has ' + str(len(self.states)) +
                       ' states but was passed ' +
                       str(len(initial_state)) +
                       ' initial states.')
    timesteps = K.int_shape(inputs)[1]

    kwargs = {}
    if generic_utils.has_arg(self.cell.call, 'training'):
      kwargs['training'] = training

    if constants:
      if not generic_utils.has_arg(self.cell.call, 'constants'):
        raise ValueError('RNN cell does not support constants')

      def step(inputs, states):
        constants = states[-self._num_constants:]
        states = states[:-self._num_constants]
        return self.cell.call(inputs, states, constants=constants,
                              **kwargs)
    else:
      def step(inputs, states):
        return self.cell.call(inputs, states, **kwargs)

    last_output, outputs, states = K.rnn(step,
                                         inputs,
                                         initial_state,
                                         constants=constants,
                                         go_backwards=self.go_backwards,
                                         mask=mask,
                                         input_length=timesteps)
    if self.stateful:
      updates = []
      for i in range(len(states)):
        updates.append(K.update(self.states[i], states[i]))
      self.add_update(updates)

    if self.return_sequences:
      output = outputs
    else:
      output = last_output

    if self.return_state:
      if not isinstance(states, (list, tuple)):
        states = [states]
      else:
        states = list(states)
      return [output] + states
    else:
      return output

  def reset_states(self, states=None):
    if not self.stateful:
      raise AttributeError('Layer must be stateful.')
    input_shape = self.input_spec[0].shape
    state_shape = self.compute_output_shape(input_shape)
    if self.return_state:
      state_shape = state_shape[0]
    if self.return_sequences:
      state_shape = state_shape[:1].concatenate(state_shape[2:])
    if None in state_shape:
      raise ValueError('If a RNN is stateful, it needs to know '
                       'its batch size. Specify the batch size '
                       'of your input tensors: \n'
                       '- If using a Sequential model, '
                       'specify the batch size by passing '
                       'a `batch_input_shape` '
                       'argument to your first layer.\n'
                       '- If using the functional API, specify '
                       'the time dimension by passing a '
                       '`batch_shape` argument to your Input layer.\n'
                       'The same thing goes for the number of rows and '
                       'columns.')

    # helper function
    def get_tuple_shape(nb_channels):
      result = list(state_shape)
      if self.cell.data_format == 'channels_first':
        result[1] = nb_channels
      elif self.cell.data_format == 'channels_last':
        result[3] = nb_channels
      else:
        raise KeyError
      return tuple(result)

    # initialize state if None
    if self.states[0] is None:
      if hasattr(self.cell.state_size, '__len__'):
        self.states = [K.zeros(get_tuple_shape(dim),dtype=tf.float16)
                       for dim in self.cell.state_size]
      else:
        self.states = [K.zeros(get_tuple_shape(self.cell.state_size))]
    elif states is None:
      if hasattr(self.cell.state_size, '__len__'):
        for state, dim in zip(self.states, self.cell.state_size):
          K.set_value(state, np.zeros(get_tuple_shape(dim),dtype=tf.float16))
      else:
        K.set_value(self.states[0],
                    np.zeros(get_tuple_shape(self.cell.state_size)))
    else:
      if not isinstance(states, (list, tuple)):
        states = [states]
      if len(states) != len(self.states):
        raise ValueError('Layer ' + self.name + ' expects ' +
                         str(len(self.states)) + ' states, ' +
                         'but it received ' + str(len(states)) +
                         ' state values. Input received: ' + str(states))
      for index, (value, state) in enumerate(zip(states, self.states)):
        if hasattr(self.cell.state_size, '__len__'):
          dim = self.cell.state_size[index]
        else:
          dim = self.cell.state_size
        if value.shape != get_tuple_shape(dim):
          raise ValueError('State ' + str(index) +
                           ' is incompatible with layer ' +
                           self.name + ': expected shape=' +
                           str(get_tuple_shape(dim)) +
                           ', found shape=' + str(value.shape))
        # TODO(anjalisridhar): consider batch calls to `set_value`.
        K.set_value(state, value)

#ConvGRU2D
class ConvGRU2D(ConvRNN2D):
    """Convolutional GRU.

        It is similar to an GRU layer, but the input transformations
        and recurrent transformations are both convolutional.

        Arguments:
            filters: Integer, the dimensionality of the output space
            (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
            dimensions of the convolution window.
            strides: An integer or tuple/list of n integers,
            specifying the strides of the convolution.
            Specifying any stride value != 1 is incompatible with specifying
            any `dilation_rate` value != 1.
            padding: One of `"valid"` or `"same"` (case-insensitive).
            data_format: A string,
            one of `channels_last` (default) or `channels_first`.
            The ordering of the dimensions in the inputs.
            `channels_last` corresponds to inputs with shape
            `(batch, time, ..., channels)`
            while `channels_first` corresponds to
            inputs with shape `(batch, time, channels, ...)`.
            It defaults to the `image_data_format` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "channels_last".
            layer_norm: Defaults to LayerNormalization Layer to be applied to
            to output of each GRU cell, pass None if no layer_normalization is desired. 
            dilation_rate: An integer or tuple/list of n integers, specifying
            the dilation rate to use for dilated convolution.
            Currently, specifying any `dilation_rate` value != 1 is
            incompatible with specifying any `strides` value != 1.
            activation: Activation function to use.
            By default hyperbolic tangent activation function is applied
            (`tanh(x)`).
            recurrent_activation: Activation function to use
            for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
            used for the linear transformation of the inputs.
            recurrent_initializer: Initializer for the `recurrent_kernel`
            weights matrix,
            used for the linear transformation of the recurrent state.
            bias_initializer: Initializer for the bias vector.
            If True, add 1 to the bias of the forget gate at initialization.
            Use in combination with `bias_initializer="zeros"`.
            This is recommended in [Jozefowicz et al.]
            (http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            kernel_regularizer: Regularizer function applied to
            the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
            the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            activity_regularizer: Regularizer function applied to.
            kernel_constraint: Constraint function applied to
            the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
            the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            return_sequences: Boolean. Whether to return the last output
            in the output sequence, or the full sequence.
            go_backwards: Boolean (default False).
            If True, process the input sequence backwards.
            stateful: Boolean (default False). If True, the last state
            for each sample at index i in a batch will be used as initial
            state for the sample of index i in the following batch.
            dropout: Float between 0 and 1.
            Fraction of the units to drop for
            the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
            Fraction of the units to drop for
            the linear transformation of the recurrent state.

        Call arguments:
            inputs: A 5D tensor.
            mask: Binary tensor of shape `(samples, timesteps)` indicating whether
            a given timestep should be masked.
            training: Python boolean indicating whether the layer should behave in
            training mode or in inference mode. This argument is passed to the cell
            when calling it. This is only relevant if `dropout` or `recurrent_dropout`
            are set.
            initial_state: List of initial state tensors to be passed to the first
            call of the cell.

        Input shape:
            - If data_format='channels_first'
                5D tensor with shape:
                `(samples, time, channels, rows, cols)`
            - If data_format='channels_last'
                5D tensor with shape:
                `(samples, time, rows, cols, channels)`

        Output shape:
            - If `return_sequences`
            - If data_format='channels_first'
                5D tensor with shape:
                `(samples, time, filters, output_row, output_col)`
            - If data_format='channels_last'
                5D tensor with shape:
                `(samples, time, output_row, output_col, filters)`
            - Else
            - If data_format ='channels_first'
                4D tensor with shape:
                `(samples, filters, output_row, output_col)`
            - If data_format='channels_last'
                4D tensor with shape:
                `(samples, output_row, output_col, filters)`
            where `o_row` and `o_col` depend on the shape of the filter and
            the padding

        Raises:
            ValueError: in case of invalid constructor arguments.

    """
    def __init__(self,
                filters,
                    kernel_size,
                    implementation,
                    layer_norm,                
                    strides=(1, 1),
                    padding='valid',
                    data_format=None,
                    dilation_rate=(1, 1),
                    activation='tanh',
                    recurrent_activation='hard_sigmoid',
                    use_bias=True,
                    kernel_initializer='glorot_uniform',
                    recurrent_initializer='orthogonal',
                    bias_initializer='zeros',
                    kernel_regularizer=None,
                    recurrent_regularizer=None,
                    bias_regularizer=None,
                    activity_regularizer=None,
                    kernel_constraint=None,
                    recurrent_constraint=None,
                    bias_constraint=None,
                    return_sequences=False,
                    go_backwards=False,
                    stateful=False,
                    dropout=0.,
                    recurrent_dropout=0.,
                    reset_after=True,
                    **kwargs):
        
        self.layer_norm = layer_norm
        if self.layer_norm == None:
            self.bool_ln = False
        else:
            self.bool_ln = True
            self.layer_norm._dtype =  "float32"

        cell = ConvGRU2DCell(filters=filters,
                            kernel_size=kernel_size,
                                strides=strides,
                                padding=padding,
                                data_format=data_format,
                                dilation_rate=dilation_rate,
                                layer_norm=self.layer_norm,
                                bool_ln=self.bool_ln,
                                activation=activation,
                                recurrent_activation=recurrent_activation,
                                use_bias=use_bias,
                                kernel_initializer=kernel_initializer,
                                recurrent_initializer=recurrent_initializer,
                                bias_initializer=bias_initializer,
                                kernel_regularizer=kernel_regularizer,
                                recurrent_regularizer=recurrent_regularizer,
                                bias_regularizer=bias_regularizer,
                                kernel_constraint=kernel_constraint,
                                recurrent_constraint=recurrent_constraint,
                                bias_constraint=bias_constraint,
                                dropout=dropout,
                                recurrent_dropout=recurrent_dropout,
                                implementation=implementation,
                                reset_after=reset_after,
                                dtype=kwargs.get('dtype'))
        
        super(ConvGRU2D, self).__init__(cell,
                                        return_sequences=return_sequences,
                                        go_backwards=go_backwards,
                                        stateful=stateful,
                                        **kwargs)
        self.activity_regularizer = regularizers.get(activity_regularizer)
    
    def call(self, inputs, mask=None, training=None, initial_state=None):
        self._maybe_reset_cell_dropout_mask(self.cell)
        return super(ConvGRU2D, self).call(inputs,
                                            mask=mask,
                                            training=training,
                                            initial_state=initial_state)
    #region
    @property
    def filters(self):
        return self.cell.filters

    @property
    def kernel_size(self):
        return self.cell.kernel_size

    @property
    def strides(self):
        return self.cell.strides

    @property
    def padding(self):
        return self.cell.padding

    @property
    def data_format(self):
        return self.cell.data_format

    @property
    def dilation_rate(self):
        return self.cell.dilation_rate

    @property
    def activation(self):
        return self.cell.activation

    @property
    def recurrent_activation(self):
        return self.cell.recurrent_activation

    @property
    def use_bias(self):
        return self.cell.use_bias

    @property
    def kernel_initializer(self):
        return self.cell.kernel_initializer

    @property
    def recurrent_initializer(self):
        return self.cell.recurrent_initializer

    @property
    def bias_initializer(self):
        return self.cell.bias_initializer

    @property
    def kernel_regularizer(self):
        return self.cell.kernel_regularizer

    @property
    def recurrent_regularizer(self):
        return self.cell.recurrent_regularizer

    @property
    def bias_regularizer(self):
        return self.cell.bias_regularizer

    @property
    def kernel_constraint(self):
        return self.cell.kernel_constraint

    @property
    def recurrent_constraint(self):
        return self.cell.recurrent_constraint

    @property
    def bias_constraint(self):
        return self.cell.bias_constraint

    @property
    def dropout(self):
        return self.cell.dropout

    @property
    def recurrent_dropout(self):
        return self.cell.recurrent_dropout
    
    @property
    def implementation(self):
        return self.cell.implementation


    def get_config(self):
        config = {'filters': self.filters,
                'kernel_size': self.kernel_size,
                'strides': self.strides,
                'padding': self.padding,
                'data_format': self.data_format,
                'dilation_rate': self.dilation_rate,
                'activation': activations.serialize(self.activation),
                'recurrent_activation': activations.serialize(
                    self.recurrent_activation),
                'use_bias': self.use_bias,
                'kernel_initializer': initializers.serialize(
                    self.kernel_initializer),
                'recurrent_initializer': initializers.serialize(
                    self.recurrent_initializer),
                'bias_initializer': initializers.serialize(self.bias_initializer),
                'kernel_regularizer': regularizers.serialize(
                    self.kernel_regularizer),
                'recurrent_regularizer': regularizers.serialize(
                    self.recurrent_regularizer),
                'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                'activity_regularizer': regularizers.serialize(
                    self.activity_regularizer),
                'kernel_constraint': constraints.serialize(
                    self.kernel_constraint),
                'recurrent_constraint': constraints.serialize(
                    self.recurrent_constraint),
                'bias_constraint': constraints.serialize(self.bias_constraint),
                'dropout': self.dropout,
                'recurrent_dropout': self.recurrent_dropout,
                'layer_norm':self.layer_norm,
                'implementation':self.implementation }

        base_config = super(ConvGRU2D, self).get_config()
        del base_config['cell']
        return dict(list(base_config.items()) + list(config.items()))

    @classmethod
    def from_config(cls, config):
        return cls(**config)
    #endregion

    def get_initial_state(self, inputs):
        
        initial_state = K.zeros_like(inputs)
        # (samples, rows, cols, filters)
        initial_state = K.sum(initial_state, axis=1)

        shape_h_state = list(self.cell.kernel_shape)
        shape_h_state[-1] = self.cell.filters

        
        initial_hidden_state = self.cell.input_conv(initial_state,
                                            array_ops.zeros(tuple(shape_h_state) , self._compute_dtype),
                                            padding=self.cell.padding)
        

        if hasattr(self.cell.state_size, '__len__'):
            return [initial_hidden_state ]
        else:
            return [initial_hidden_state]

class ConvGRU2DCell(DropoutRNNCellMixin, Layer):
    """Cell class for the ConvGRU2D layer.

        Arguments:
            filters: Integer, the dimensionality of the output space
            (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
            dimensions of the convolution window.
            strides: An integer or tuple/list of n integers,
            specifying the strides of the convolution.
            Specifying any stride value != 1 is incompatible with specifying
            any `dilation_rate` value != 1.
            padding: One of `"valid"` or `"same"` (case-insensitive).
            data_format: A string,
            one of `channels_last` (default) or `channels_first`.
            It defaults to the `image_data_format` value found in your
            Keras config file at `~/.keras/keras.json`.
            If you never set it, then it will be "channels_last".
            dilation_rate: An integer or tuple/list of n integers, specifying
            the dilation rate to use for dilated convolution.
            Currently, specifying any `dilation_rate` value != 1 is
            incompatible with specifying any `strides` value != 1.
            activation: Activation function to use.
            If you don't specify anything, no activation is applied
            (ie. "linear" activation: `a(x) = x`).
            recurrent_activation: Activation function to use
            for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
            used for the linear transformation of the inputs.
            recurrent_initializer: Initializer for the `recurrent_kernel`
            weights matrix,
            used for the linear transformation of the recurrent state.
            bias_initializer: Initializer for the bias vector.
            If True, add 1 to the bias of the forget gate at initialization.
            Use in combination with `bias_initializer="zeros"`.
            This is recommended in [Jozefowicz et al.]
            (http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            kernel_regularizer: Regularizer function applied to
            the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
            the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            kernel_constraint: Constraint function applied to
            the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
            the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            dropout: Float between 0 and 1.
            Fraction of the units to drop for
            the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
            Fraction of the units to drop for
            the linear transformation of the recurrent state.

            Call arguments:
                inputs: A 4D tensor.
                states:  List of state tensors corresponding to the previous timestep.
                training: Python boolean indicating whether the layer should behave in
                training mode or in inference mode. Only relevant when `dropout` or
                `recurrent_dropout` is used.
        
    """

    def __init__(self,
                filters,
                    kernel_size,
                    layer_norm,
                    bool_ln,
                    strides=(1, 1),
                    padding='valid',
                    data_format=None,
                    dilation_rate=(1, 1),
                    activation='tanh',
                    recurrent_activation='hard_sigmoid',
                    use_bias=True,
                    kernel_initializer='glorot_uniform',
                    recurrent_initializer='orthogonal',
                    bias_initializer='zeros',
                    kernel_regularizer=None,
                    recurrent_regularizer=None,
                    bias_regularizer=None,
                    kernel_constraint=None,
                    recurrent_constraint=None,
                    bias_constraint=None,
                    dropout=0.,
                    recurrent_dropout=0.,
                    implementation=1,
                    reset_after= False,
                    **kwargs):
        super(ConvGRU2DCell, self).__init__(**kwargs)
        self.filters = filters
        self.kernel_size = conv_utils.normalize_tuple(kernel_size, 2, 'kernel_size')
        self.strides                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 = conv_utils.normalize_tuple(strides, 2, 'strides')
        self.padding = conv_utils.normalize_padding(padding)
        self.data_format = conv_utils.normalize_data_format(data_format)
        self.dilation_rate = conv_utils.normalize_tuple(dilation_rate, 2,
                                                        'dilation_rate')
        self.activation = activations.get(activation)
        self.recurrent_activation = activations.get(recurrent_activation)
        self.use_bias = use_bias

        self.layer_norm = layer_norm
        self.bool_ln = bool_ln
               
        self.kernel_initializer = initializers.get(kernel_initializer)
        self.recurrent_initializer = initializers.get(recurrent_initializer)
        self.bias_initializer = initializers.get(bias_initializer)

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.dropout = min(1., max(0., dropout))
        self.recurrent_dropout = min(1., max(0., recurrent_dropout))
        self.state_size = (self.filters)

        self.implementation = implementation
        self.reset_after = reset_after

    def build(self, input_shape):
        #TODO: add cudnn version using code for tf.keras.layers.GRU
        if self.data_format == 'channels_first':
            channel_axis = 1
        else:
            channel_axis = -1
        if input_shape[channel_axis] is None:
            raise ValueError('The channel dimension of the inputs '
                        'should be defined. Found `None`.')
        input_dim = input_shape[channel_axis]
        
        kernel_shape = self.kernel_size + (input_dim, self.filters * 3)
        self.kernel_shape = kernel_shape
        #recurrent_kernel_shape = self.kernel_size + (self.filters, self.filters * 4)
        recurrent_kernel_shape = self.kernel_size + (self.filters, self.filters * 3)

        self.kernel = self.add_weight(shape=kernel_shape,
                                    initializer=self.kernel_initializer,
                                    name='kernel',
                                    regularizer=self.kernel_regularizer,
                                    constraint=self.kernel_constraint)
        self.recurrent_kernel = self.add_weight(
            shape=recurrent_kernel_shape,
            initializer=self.recurrent_initializer,
            name='recurrent_kernel',
            regularizer=self.recurrent_regularizer,
            constraint=self.recurrent_constraint)

        if self.use_bias:
            bias_initializer = self.bias_initializer
            if self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3*2,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)
            elif not self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)
        else:
            self.bias = None
        self.built = True

    #@tf.function
    def call(self, inputs, states, training=None):
        h_tm1 = tf.cast(states[0],dtype=inputs.dtype) # previous memory state
            # dropout matrices for input units
        dp_mask = self.get_dropout_mask_for_cell(inputs, training, count=3)
            # dropout matrices for recurrent units
        rec_dp_mask = self.get_recurrent_dropout_mask_for_cell(
            h_tm1, training, count=3)

        if self.use_bias:
            if not self.reset_after:
                bias_z, bias_r, bias_h = array_ops.split(self.bias, 3)
                bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

            elif self.reset_after:
                bias_z, bias_r, bias_h, bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = array_ops.split(self.bias, 3*2)

        else:
            bias_z, bias_r, bias_h = None, None, None
            bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

        if self.implementation==1:

            if 0 < self.dropout < 1.:
                inputs_z = inputs * dp_mask[0]
                inputs_r = inputs * dp_mask[1]
                inputs_h = inputs * dp_mask[2]
            else:
                inputs_z = inputs
                inputs_r = inputs
                inputs_h = inputs

            (kernel_z, kernel_r, kernel_h) = array_ops.split(self.kernel, 3, axis=3)

            x_z = self.input_conv(inputs_z, kernel_z, bias_z, padding=self.padding)
            x_r = self.input_conv(inputs_r, kernel_r, bias_r, padding=self.padding)
            x_h = self.input_conv(inputs_h, kernel_h, bias_h, padding=self.padding)
                        
            if 0 < self.recurrent_dropout < 1.:
                h_tm1_z = h_tm1 * rec_dp_mask[0]
                h_tm1_r = h_tm1 * rec_dp_mask[1]
                h_tm1_h = h_tm1 * rec_dp_mask[2]
            else:
                h_tm1_z = h_tm1
                h_tm1_r = h_tm1
                h_tm1_h = h_tm1

            (recurrent_kernel_z,
                recurrent_kernel_r,
                recurrent_kernel_h) = array_ops.split(self.recurrent_kernel, 3, axis=3)
            
            recurrent_z = self.recurrent_conv(h_tm1_z, recurrent_kernel_z)
            recurrent_r = self.recurrent_conv(h_tm1_r, recurrent_kernel_r)

            if self.reset_after and self.use_bias:
                recurrent_z = K.bias_add( recurrent_z, bias_z_rcrnt )
                recurrent_r = K.bias_add( recurrent_r, bias_r_rcrnt )

            z = self.recurrent_activation(x_z + recurrent_z)
            r = self.recurrent_activation(x_r + recurrent_r)

            # reset gate applied after/before matrix multiplication
            if self.reset_after:
                recurrent_h = self.recurrent_conv(h_tm1_h, recurrent_kernel_h)
                if self.use_bias:
                    recurrent_h = K.bias_add( recurrent_h, bias_h_rcrnt)
                recurrent_h = r * recurrent_h
            else:
                recurrent_h = self.recurrent_conv( r*h_tm1_h, recurrent_kernel_h )
            
            hh = self.activation( x_h + recurrent_h ) #two state structure will have to be added here for cell_custom

        elif self.implementation ==2 :
            raise NotImplementedError
        
        if self.bool_ln:
            hh = tf.cast( self.layer_norm(hh), self._compute_dtype)

        h = z*h_tm1 + (1-z)*hh
        
        return h, [h]

    #@tf.function
    def input_conv(self, x, w, b=None, padding='valid'):
        conv_out = K.conv2d(x, w, strides=self.strides,
                            padding=padding,
                            data_format=self.data_format,
                            dilation_rate=self.dilation_rate)
        if b is not None:
            conv_out = K.bias_add(conv_out, b,
                                data_format=self.data_format)
        return conv_out

    def recurrent_conv(self, x, w):
        conv_out = K.conv2d(x, w, strides=(1, 1),
                            padding='same',
                            data_format=self.data_format)
        return conv_out

    def get_config(self):
        config = {'filters': self.filters,
                'kernel_size': self.kernel_size,
                'strides': self.strides,
                'padding': self.padding,
                'data_format': self.data_format,
                'dilation_rate': self.dilation_rate,
                'activation': activations.serialize(self.activation),
                'recurrent_activation': activations.serialize(
                    self.recurrent_activation),
                'use_bias': self.use_bias,
                'kernel_initializer': initializers.serialize(
                    self.kernel_initializer),
                'recurrent_initializer': initializers.serialize(
                    self.recurrent_initializer),
                'bias_initializer': initializers.serialize(self.bias_initializer),
                'kernel_regularizer': regularizers.serialize(
                    self.kernel_regularizer),
                'recurrent_regularizer': regularizers.serialize(
                    self.recurrent_regularizer),
                'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                'kernel_constraint': constraints.serialize(
                    self.kernel_constraint),
                'recurrent_constraint': constraints.serialize(
                    self.recurrent_constraint),
                'bias_constraint': constraints.serialize(self.bias_constraint),
                'dropout': self.dropout,
                'recurrent_dropout': self.recurrent_dropout,
                'implementation':self.implementation,
                'layer_norm':self.layer_norm,
                'bool_ln':self.bool_ln,
                'reset_after':self.reset_after }

        base_config = super(ConvGRU2DCell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

# region --- 2 Cell Conv GRU layer [Decoder layers]
class ConvGRU2D_Dualcell(ConvRNN2D):
    """
        Dual cell Convolutional GRU that allows two inputs.

        Arguments:
            filters: Integer, the dimensionality of the output space
                (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
                dimensions of the convolution window.
            strides: An integer or tuple/list of n integers,
                specifying the strides of the convolution.
                Specifying any stride value != 1 is incompatible with specifying
                any `dilation_rate` value != 1.
            padding: One of `"valid"` or `"same"` (case-insensitive).
            data_format: A string,
                one of `channels_last` (default) or `channels_first`.
                The ordering of the dimensions in the inputs.
                `channels_last` corresponds to inputs with shape
                `(batch, time, ..., channels)`
                while `channels_first` corresponds to
                inputs with shape `(batch, time, channels, ...)`.
                It defaults to the `image_data_format` value found in your
                Keras config file at `~/.keras/keras.json`.
                If you never set it, then it will be "channels_last".
            dilation_rate: An integer or tuple/list of n integers, specifying
                the dilation rate to use for dilated convolution.
                Currently, specifying any `dilation_rate` value != 1 is
                incompatible with specifying any `strides` value != 1.
            activation: Activation function to use.
                By default hyperbolic tangent activation function is applied
                (`tanh(x)`).
            recurrent_activation: Activation function to use
                for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
                used for the linear transformation of the inputs.
            recurrent_initializer: Initializer for the `recurrent_kernel`
                weights matrix,
                used for the linear transformation of the recurrent state.
            bias_initializer: Initializer for the bias vector.
            
                If True, add 1 to the bias of the forget gate at initialization.
                Use in combination with `bias_initializer="zeros"`.
                This is recommended in [Jozefowicz et al.]
                (http://www.jmlr.org/proceedings/papers/v37/jozefowicz15.pdf)
            kernel_regularizer: Regularizer function applied to
                the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
                the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            activity_regularizer: Regularizer function applied to.
            kernel_constraint: Constraint function applied to
                the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
                the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            return_sequences: Boolean. Whether to return the last output
                in the output sequence, or the full sequence.
            go_backwards: Boolean (default False).
                If True, process the input sequence backwards.
            stateful: Boolean (default False). If True, the last state
                for each sample at index i in a batch will be used as initial
                state for the sample of index i in the following batch.
            dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the recurrent state.
        Call arguments:
            inputs: A 5D tensor.
            mask: Binary tensor of shape `(samples, timesteps)` indicating whether
                a given timestep should be masked.
            training: Python boolean indicating whether the layer should behave in
                training mode or in inference mode. This argument is passed to the cell
                when calling it. This is only relevant if `dropout` or `recurrent_dropout`
                are set.
            initial_state: List of initial state tensors to be passed to the first
                call of the cell.
        Input shape:
            - If data_format='channels_first'
                    5D tensor with shape:
                    `(samples, time, channels, rows, cols)`
            - If data_format='channels_last'
                    5D tensor with shape:
                    `(samples, time, rows, cols, channels)`
        Output shape:
            - If `return_sequences`
                    - If data_format='channels_first'
                        5D tensor with shape:
                        `(samples, time, filters, output_row, output_col)`
                    - If data_format='channels_last'
                        5D tensor with shape:
                        `(samples, time, output_row, output_col, filters)`
            - Else
                - If data_format ='channels_first'
                        4D tensor with shape:
                        `(samples, filters, output_row, output_col)`
                - If data_format='channels_last'
                        4D tensor with shape:
                        `(samples, output_row, output_col, filters)`
                where `o_row` and `o_col` depend on the shape of the filter and
                the padding
        Raises:
            ValueError: in case of invalid constructor arguments.
    """

    def __init__(self,
                 filters,
                    kernel_size,
                    implementation,
                    layer_norm,
                    strides=(1, 1),
                    padding='valid',
                    data_format=None,
                    dilation_rate=(1, 1),
                    activation='tanh',
                    recurrent_activation='hard_sigmoid',
                    use_bias=True,
                    kernel_initializer='glorot_uniform',
                    recurrent_initializer='orthogonal',
                    bias_initializer='zeros',
                    kernel_regularizer=None,
                    recurrent_regularizer=None,
                    bias_regularizer=None,
                    activity_regularizer=None,
                    kernel_constraint=None,
                    recurrent_constraint=None,
                    bias_constraint=None,
                    return_sequences=False,
                    go_backwards=False,
                    stateful=False,
                    dropout=0.,
                    recurrent_dropout=0.,
                    reset_after=False,
                    **kwargs):
        #layer norm
        bool_ln = False
        self.layer_norm = layer_norm
        self.activity_regularizer = regularizers.get(activity_regularizer)
        
        cell = ConvGRU2DCell_Dualcell(filters=filters,
                                     kernel_size=kernel_size,
                                     strides=strides,
                                     padding=padding,
                                     data_format=data_format,
                                     dilation_rate=dilation_rate,
                                     implementation=implementation,
                                     layer_norm=self.layer_norm,
                                     bool_ln = bool_ln,
                                     activation=activation,
                                     recurrent_activation=recurrent_activation,
                                     use_bias=use_bias,
                                     kernel_initializer=kernel_initializer,
                                     recurrent_initializer=recurrent_initializer,
                                     bias_initializer=bias_initializer,
                                     kernel_regularizer=kernel_regularizer,
                                     recurrent_regularizer=recurrent_regularizer,
                                     bias_regularizer=bias_regularizer,
                                     kernel_constraint=kernel_constraint,
                                     recurrent_constraint=recurrent_constraint,
                                     bias_constraint=bias_constraint,
                                     dropout=dropout,
                                     recurrent_dropout=recurrent_dropout,
                                     reset_after=reset_after,
                                     dtype=kwargs.get('dtype'))

        super(ConvGRU2D_Dualcell, self).__init__(cell,
                                                return_sequences=return_sequences,
                                                go_backwards=go_backwards,
                                                stateful=stateful,
                                                **kwargs)
        
    def call(self, inputs, mask=None, training=None, initial_state=None):
        self._maybe_reset_cell_dropout_mask(self.cell)

        if self.stateful and (initial_state is not None):
            initial_state = self.states
        elif (initial_state is not None) :
            initial_state = self.get_initial_state(inputs)
        
        return super(ConvGRU2D_Dualcell, self).call(inputs,
                                            mask=mask,
                                            training=training,
                                            initial_state=initial_state)


    # region --- properties / config
    @property
    def filters(self):
        return self.cell.filters

    @property
    def kernel_size(self):
        return self.cell.kernel_size

    @property
    def strides(self):
        return self.cell.strides

    @property
    def padding(self):
        return self.cell.padding

    @property
    def data_format(self):
        return self.cell.data_format

    @property
    def dilation_rate(self):
        return self.cell.dilation_rate

    @property
    def activation(self):
        return self.cell.activation

    @property
    def recurrent_activation(self):
        return self.cell.recurrent_activation

    @property
    def use_bias(self):
        return self.cell.use_bias

    @property
    def kernel_initializer(self):
        return self.cell.kernel_initializer

    @property
    def recurrent_initializer(self):
        return self.cell.recurrent_initializer

    @property
    def bias_initializer(self):
        return self.cell.bias_initializer

    # @property
    # def unit_forget_bias(self):
    #     return self.cell.unit_forget_bias

    @property
    def kernel_regularizer(self):
        return self.cell.kernel_regularizer

    @property
    def recurrent_regularizer(self):
        return self.cell.recurrent_regularizer

    @property
    def bias_regularizer(self):
        return self.cell.bias_regularizer

    @property
    def kernel_constraint(self):
        return self.cell.kernel_constraint

    @property
    def recurrent_constraint(self):
        return self.cell.recurrent_constraint

    @property
    def bias_constraint(self):
        return self.cell.bias_constraint

    @property
    def dropout(self):
        return self.cell.dropout

    @property
    def recurrent_dropout(self):
        return self.cell.recurrent_dropout
    
    @property
    def implementation(self):
        return self.cell.implementation
    

    def get_config(self):
        config = {'filters': self.filters,
                  'kernel_size': self.kernel_size,
                  'strides': self.strides,
                  'padding': self.padding,
                  'data_format': self.data_format,
                  'dilation_rate': self.dilation_rate,
                  'activation': activations.serialize(self.activation),
                  'recurrent_activation': activations.serialize(
                      self.recurrent_activation),
                  'use_bias': self.use_bias,
                  'kernel_initializer': initializers.serialize(
                      self.kernel_initializer),
                  'recurrent_initializer': initializers.serialize(
                      self.recurrent_initializer),
                  'bias_initializer': initializers.serialize(self.bias_initializer),
                  'kernel_regularizer': regularizers.serialize(
                      self.kernel_regularizer),
                  'recurrent_regularizer': regularizers.serialize(
                      self.recurrent_regularizer),
                  'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                  'activity_regularizer': regularizers.serialize(
                      self.activity_regularizer),
                  'kernel_constraint': constraints.serialize(
                      self.kernel_constraint),
                  'recurrent_constraint': constraints.serialize(
                      self.recurrent_constraint),
                  'bias_constraint': constraints.serialize(self.bias_constraint),
                  'dropout': self.dropout,
                  'recurrent_dropout': self.recurrent_dropout,
                  'implementation':self.implementation,
                  'layer_norm':self.layer_norm}
        base_config = super(ConvGRU2D_Dualcell, self).get_config()
        del base_config['cell']
        return dict(list(base_config.items()) + list(config.items()))
    

    @classmethod
    def from_config(cls, config):
        return cls(**config)
    # endregion 

    def get_initial_state(self, inputs):
        
        initial_state = K.zeros_like(inputs)

        initial_state = K.sum(initial_state, axis=1)

        shape_h_state = list(self.cell.kernel_shape)
        shape_h_state[-1] = self.cell.filters
        
        initial_hidden_state = self.cell.input_conv(initial_state,
                                            array_ops.zeros(tuple(shape_h_state) , self._compute_dtype),
                                            padding=self.cell.padding)

        if hasattr(self.cell.state_size, '__len__'):
            return [initial_hidden_state ]
        else:
            return [initial_hidden_state]

class ConvGRU2DCell_Dualcell(DropoutRNNCellMixin, Layer):
    """
        Cell class for the ConvGRU2D layer.
        Arguments:
            filters: Integer, the dimensionality of the output space
                (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
                dimensions of the convolution window.
            strides: An integer or tuple/list of n integers,
                specifying the strides of the convolution.
                Specifying any stride value != 1 is incompatible with specifying
                any `dilation_rate` value != 1.
            padding: One of `"valid"` or `"same"` (case-insensitive).
                data_format: A string,
                one of `channels_last` (default) or `channels_first`.
            dilation_rate: An integer or tuple/list of n integers, specifying
                the dilation rate to use for dilated convolution.
                Currently, specifying any `dilation_rate` value != 1 is
                incompatible with specifying any `strides` value != 1.
                activation: Activation function to use.
            recurrent_activation: Activation function to use
                for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
                used for the linear transformation of the inputs.
                recurrent_initializer: Initializer for the `recurrent_kernel`
                weights matrix,
                used for the linear transformation of the recurrent state.
                bias_initializer: Initializer for the bias vector.
            kernel_regularizer: Regularizer function applied to
                the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
                the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            kernel_constraint: Constraint function applied to
                the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
                the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the recurrent state.
        Call arguments:
            inputs: A 4D tensor.
            states:  List of state tensors corresponding to the previous timestep.
            training: Python boolean indicating whether the layer should behave in
            training mode or in inference mode. Only relevant when `dropout` or
            `recurrent_dropout` is used.
    """

    def __init__(self,
               filters,
                    kernel_size,
                    layer_norm,
                    bool_ln,
                    implementation,
                    reset_after=False,
                    strides=(1, 1),
                    padding='valid',
                    data_format=None,
                    dilation_rate=(1, 1),
                    activation='tanh',
                    recurrent_activation='hard_sigmoid',
                    use_bias=True,
                    kernel_initializer='glorot_uniform',
                    recurrent_initializer='orthogonal',
                    bias_initializer='zeros',
                    
                    kernel_regularizer=None,
                    recurrent_regularizer=None,
                    bias_regularizer=None,
                    kernel_constraint=None,
                    recurrent_constraint=None,
                    bias_constraint=None,
                    dropout=0.,
                    recurrent_dropout=0.,
                    **kwargs):
        super(ConvGRU2DCell_Dualcell, self).__init__(**kwargs)
        self.filters = filters
        self.kernel_size = conv_utils.normalize_tuple(kernel_size, 2, 'kernel_size')
        self.strides = conv_utils.normalize_tuple(strides, 2, 'strides')
        self.padding = conv_utils.normalize_padding(padding)
        self.data_format = conv_utils.normalize_data_format(data_format)
        self.dilation_rate = conv_utils.normalize_tuple(dilation_rate, 2,
                                                        'dilation_rate')
        self.activation = activations.get(activation)
        self.recurrent_activation = activations.get(recurrent_activation)
        self.use_bias = use_bias

        self.layer_norm = layer_norm
        self.bool_ln = bool_ln

        self.kernel_initializer = initializers.get(kernel_initializer)
        self.recurrent_initializer = initializers.get(recurrent_initializer)
        self.bias_initializer = initializers.get(bias_initializer)

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.dropout = min(1., max(0., dropout))
        self.recurrent_dropout = min(1., max(0., recurrent_dropout))
        self.state_size = (self.filters)

        self.implementation = implementation
        self.reset_after = reset_after

    def build(self, input_shape):
        # Intializing training weights
        if self.data_format == 'channels_first':
            channel_axis = 1
        else:
            channel_axis = -1
        if input_shape[channel_axis] is None:
            raise ValueError('The channel dimension of the inputs '
                            'should be defined. Found `None`.')
        input_dim = input_shape[channel_axis]
        
        
        kernel_shape = self.kernel_size + (input_dim, self.filters * 3) #Changed here
        self.kernel_shape = kernel_shape
        self.corrected_kernel_shape = tf.TensorShape(self.kernel_size + (input_dim//2, self.filters * 6) )

        recurrent_kernel_shape = self.kernel_size + (self.filters, self.filters * 3) #This stays at 3 for 2 input gru

        self.kernel = self.add_weight(shape=self.corrected_kernel_shape,
                                    initializer=self.kernel_initializer,
                                    name='kernel',
                                    regularizer=self.kernel_regularizer,
                                    constraint=self.kernel_constraint)

        self.recurrent_kernel = self.add_weight(
            shape=recurrent_kernel_shape,
            initializer=self.recurrent_initializer,
            name='recurrent_kernel',
            regularizer=self.recurrent_regularizer,
            constraint=self.recurrent_constraint)

        if self.use_bias:
            bias_initializer = self.bias_initializer
            if self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3*3,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)
            elif not self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3*2,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)

        else:
            self.bias = None
        self.built = True

    def call(self, inputs, states, training=None):
        """Link To Paper: Dual State ConvGRU
            Note: self.reset_after==False methodology is explained in paper

        """
        # inputs1 and inputs 2
        inputs1, inputs2 = tf.split( inputs, 2, axis=-1)

        # previous hidden state state
        h_tm1 = tf.cast( states[0], dtype=inputs.dtype) 
        
        #dropout masks
        dp_mask1 = self.get_dropout_mask_for_cell(inputs1, training, count=3)
        dp_mask2 = self.get_dropout_mask_for_cell(inputs2, training, count=3)
        rec_dp_mask = self.get_recurrent_dropout_mask_for_cell(h_tm1, training, count=3)

        # retreive bias units
        if self.use_bias:
            if not self.reset_after:
                
                bias_z1, bias_z2, bias_r1, bias_r2, bias_h1, bias_h2 = array_ops.split(self.bias, 3*2)
                bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

            elif self.reset_after:
                bias_z1, bias_z2, bias_r1, bias_r2, bias_h1, bias_h2, bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = array_ops.split(self.bias, 3*2)

        else:
            bias_z1, bias_z2, bias_r1, bias_r2, bias_h1, bias_h2 = None, None, None, None, None, None
            bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

        
        if self.implementation == 1:
            # Applying dropout masks
            if 0 < self.dropout < 1. and training:
                inputs_z1 = inputs1 * dp_mask1[0]
                inputs_r1 = inputs1 * dp_mask1[1]
                inputs_h1 = inputs1 * dp_mask1[2]

                inputs_z2 = inputs2 * dp_mask2[0]
                inputs_r2 = inputs2 * dp_mask2[1]
                inputs_h2 = inputs2 * dp_mask2[2]

            else:
                inputs_z1 = inputs1 
                inputs_r1 = inputs1 
                inputs_h1 = inputs1 

                inputs_z2 = inputs2 
                inputs_r2 = inputs2 
                inputs_h2 = inputs2 

            # Retreiving input kernels/weight matrices
            (kernel_z1, kernel_z2, 
            kernel_r1, kernel_r2,
            kernel_h1, kernel_h2) = array_ops.split(self.kernel, 6, axis=3)

            # Calculating input part of gates
            inp_z1 = self.input_conv(inputs_z1, kernel_z1, bias_z1, padding=self.padding)
            inp_z2 = self.input_conv(inputs_z2, kernel_z2, bias_z2, padding=self.padding)
            inp_r1 = self.input_conv(inputs_r1, kernel_r1, bias_r1, padding=self.padding)
            inp_r2 = self.input_conv(inputs_r2, kernel_r2, bias_r2, padding=self.padding)
            inp_h1 = self.input_conv(inputs_h1, kernel_h1, bias_h1, padding=self.padding)
            inp_h2 = self.input_conv(inputs_h2, kernel_h2, bias_h2, padding=self.padding)

            # Applying recurrent dropout mask
            if 0 < self.recurrent_dropout < 1. and training:
                h_tm1_z = h_tm1 * rec_dp_mask[0]
                h_tm1_r = h_tm1 * rec_dp_mask[1]
                h_tm1_h = h_tm1 * rec_dp_mask[2]

            else:
                h_tm1_z = h_tm1
                h_tm1_r = h_tm1
                h_tm1_h = h_tm1
            
            # Retreiving recurrent kernels
            (recurrent_kernel_z,
                recurrent_kernel_r,
                recurrent_kernel_h) = array_ops.split(self.recurrent_kernel, 3, axis=3)
            
            # Calculating recurrent part of gates
            recurrent_z = self.recurrent_conv(h_tm1_z, recurrent_kernel_z)
            recurrent_r = self.recurrent_conv(h_tm1_r, recurrent_kernel_r)

            if self.reset_after and self.use_bias:
                recurrent_z = K.bias_add( recurrent_z, bias_z_rcrnt )
                recurrent_r = K.bias_add( recurrent_r, bias_r_rcrnt )

            #calculating gates z, r 
            z1 = self.recurrent_activation(inp_z1 + recurrent_z)
            z2 = self.recurrent_activation(inp_z2 + recurrent_z)
            r1 = self.recurrent_activation(inp_r1 + recurrent_r)
            r2 = self.recurrent_activation(inp_r2 + recurrent_r)

            # calculating gate \tilde{h} #Link To Equation(Note we)
            if self.reset_after:
                recurrent_h = self.recurrent_conv(h_tm1_h, recurrent_kernel_h)
                if self.use_bias:
                    recurrent_h = K.bias_add( recurrent_h, bias_h_rcrnt)
                recurrent_h1 = r1 * recurrent_h 
                recurrent_h2 = r2 * recurrent_h

            else:
                recurrent_h1 = self.recurrent_conv( r1*h_tm1_h, recurrent_kernel_h )
                recurrent_h2 = self.recurrent_conv( r2*h_tm1_h, recurrent_kernel_h )

            hh1 = self.activation( inp_h1 + recurrent_h1 ) #two state structure will have to be added here for cell_custom
            hh2 = self.activation( inp_h2 + recurrent_h2 )

        elif self.implementation ==2 :
            raise NotImplementedError
            
        h = ((z1+z2)/2)*h_tm1 + (1-z1)*hh1 + (1-z2)*hh2
        
        return h, [h]

    def input_conv(self, x, w, b=None, padding='valid'):
        conv_out = K.conv2d(x, w, strides=self.strides,
                            padding=padding,
                            data_format=self.data_format,
                            dilation_rate=self.dilation_rate)
        if b is not None:
            conv_out = K.bias_add(conv_out, b,
                                data_format=self.data_format)
        return conv_out

    def recurrent_conv(self, x, w):
        conv_out = K.conv2d(x, w, strides=(1, 1),
                            padding='same',
                            data_format=self.data_format)
        return conv_out

    def get_config(self):
        config = {'filters': self.filters,
                'kernel_size': self.kernel_size,
                'strides': self.strides,
                'padding': self.padding,
                'data_format': self.data_format,
                'dilation_rate': self.dilation_rate,
                'activation': activations.serialize(self.activation),
                'recurrent_activation': activations.serialize(
                    self.recurrent_activation),
                'use_bias': self.use_bias,
                'kernel_initializer': initializers.serialize(
                    self.kernel_initializer),
                'recurrent_initializer': initializers.serialize(
                    self.recurrent_initializer),
                'bias_initializer': initializers.serialize(self.bias_initializer),
                'kernel_regularizer': regularizers.serialize(
                    self.kernel_regularizer),
                'recurrent_regularizer': regularizers.serialize(
                    self.recurrent_regularizer),
                'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                'kernel_constraint': constraints.serialize(
                    self.kernel_constraint),
                'recurrent_constraint': constraints.serialize(
                    self.recurrent_constraint),
                'bias_constraint': constraints.serialize(self.bias_constraint),
                'dropout': self.dropout,
                'recurrent_dropout': self.recurrent_dropout,
                'implementation':self.implementation,
                'layer_norm':self.layer_norm,
                'bool_ln':self.bool_ln,
                'reset_after':self.reset_after 
                 }
        base_config = super(ConvGRU2DCell_Dualcell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
#endregion

# region --- Convolutional GRU w/ Inter Layer Cross Attention [Encoder Layers]
class ConvGRU2D_attn(ConvRNN2D):
    """
        ConvGRU2D w/ Inter Layer Cross Attention
        Arguments:
            filters: Integer, the dimensionality of the output space
                (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
                dimensions of the convolution window.
            implementation: int for version of ConvGRU to use
            layer_norm:                 
            attn_params: dictionary for init params for MultiHeadAttention2D
            attn_downscaling_params: dictionary for 3D averaging pooling ops
            attn_factor_reduc: int indicating the factor reduction in sequence
                                length betwee value antecedents and output
            strides: An integer or tuple/list of n integers,
                specifying the strides of the convolution.
                Specifying any stride value != 1 is incompatible with specifying
                any `dilation_rate` value != 1.
            padding: One of `"valid"` or `"same"` (case-insensitive).
            data_format: A string: `channels_last` (default) or `channels_first`
            dilation_rate: An integer or tuple/list of n integers, specifying
                the dilation rate to use for dilated convolution.
                Currently, specifying any `dilation_rate` value != 1 is
                incompatible with specifying any `strides` value != 1.
            activation: Activation function to use.
                By default hyperbolic tangent activation function is applied
                (`tanh(x)`).
            recurrent_activation: Activation function to use
                for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
                used for the linear transformation of the inputs.
            recurrent_initializer: Initializer for the `recurrent_kernel`
                weights matrix,
                used for the linear transformation of the recurrent state.
            kernel_regularizer: Regularizer function applied to
                the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
                the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            activity_regularizer: Regularizer function applied to.
            kernel_constraint: Constraint function applied to
                the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
                the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            return_sequences: Boolean. Whether to return the last output
                in the output sequence, or the full sequence.
            go_backwards: Boolean (default False).
                If True, process the input sequence backwards.
            stateful: Boolean (default False). If True, the last state
                for each sample at index i in a batch will be used as initial
                state for the sample of index i in the following batch.
            dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the recurrent state.
        Call arguments:
            inputs: A 5D tensor.
            mask: Binary tensor of shape `(samples, timesteps)` indicating whether
                a given timestep should be masked.
            training: Python boolean indicating whether the layer should behave in
                training mode or in inference mode. This argument is passed to the cell
                when calling it. This is only relevant if `dropout` or `recurrent_dropout`
                are set.
            initial_state: List of initial state tensors to be passed to the first
                call of the cell.
        Input shape:
            - If data_format='channels_first'
                    5D tensor with shape:
                    `(samples, time, channels, rows, cols)`
            - If data_format='channels_last'
                    5D tensor with shape:
                    `(samples, time, rows, cols, channels)`
        Output shape:
            - If `return_sequences`
                    - If data_format='channels_first'
                        5D tensor with shape:
                        `(samples, time, filters, output_row, output_col)`
                    - If data_format='channels_last'
                        5D tensor with shape:
                        `(samples, time, output_row, output_col, filters)`
            - Else
                - If data_format ='channels_first'
                        4D tensor with shape:
                        `(samples, filters, output_row, output_col)`
                - If data_format='channels_last'
                        4D tensor with shape:
                        `(samples, output_row, output_col, filters)`
                where `o_row` and `o_col` depend on the shape of the filter and
                the padding
        Raises:
            ValueError: in case of invalid constructor arguments.
    """

    def __init__(
                self,
                 filters,
                 kernel_size,
                 implementation,
                 layer_norm,                
                 attn_params,
                 attn_downscaling_params,
                 attn_factor_reduc,
                 strides=(1, 1),
                 padding='valid',
                 data_format=None,
                 dilation_rate=(1, 1),
                 activation='tanh',
                 recurrent_activation='hard_sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 kernel_regularizer=None,
                 recurrent_regularizer=None,
                 bias_regularizer=None,
                 activity_regularizer=None,
                 kernel_constraint=None,
                 recurrent_constraint=None,
                 bias_constraint=None,
                 return_sequences=False,
                 go_backwards=False,
                 stateful=False,
                 dropout=0.,
                 recurrent_dropout=0.,
                 trainable=True,
                 reset_after=True,
                 attn_ablation = 0,
                 **kwargs):

        bool_ln = False
        self.layer_norm = layer_norm

        
        self._trainable  = trainable
        self.attn_params = attn_params
        self.attn_downscaling_params = attn_downscaling_params
        self.attn_factor_reduc = attn_factor_reduc
        self.attn_ablation = attn_ablation

        # Ablation Studies
            # 0: Cross Attention (default)
            # 1: Averaging
            # 2: Concatenation in channel dim
            # 3: Using last element
            # 4: Sel Attention
        if self.attn_ablation in [0,4] :
            self.Attention2D = MultiHead2DAttention_v2( **attn_params, attention_scaling_params=attn_downscaling_params , attn_factor_reduc=attn_factor_reduc ,trainable=self.trainable)
        else:
            self.Attention2D = None

        # Creating recurrent cell w/ recurrent atttion included
        cell = ConvGRU2DCell_attn(filters=filters,
                                     kernel_size=kernel_size,
                                     attn_2D = self.Attention2D,
                                     attn_factor_reduc = self.attn_factor_reduc,
                                     strides=strides,
                                     padding=padding,
                                     data_format=data_format,
                                     dilation_rate=dilation_rate,
                                     layer_norm = self.layer_norm,
                                     bool_ln = bool_ln,
                                     activation=activation,
                                     recurrent_activation=recurrent_activation,
                                     use_bias=use_bias,
                                     kernel_initializer=kernel_initializer,
                                     recurrent_initializer=recurrent_initializer,
                                     bias_initializer=bias_initializer,
                                     kernel_regularizer=kernel_regularizer,
                                     recurrent_regularizer=recurrent_regularizer,
                                     bias_regularizer=bias_regularizer,
                                     kernel_constraint=kernel_constraint,
                                     recurrent_constraint=recurrent_constraint,
                                     bias_constraint=bias_constraint,
                                     dropout=dropout,
                                     recurrent_dropout=recurrent_dropout,
                                     implementation=implementation,
                                     reset_after=reset_after,
                                     dtype=kwargs.get('dtype'),
                                     attn_ablation = self.attn_ablation)

        super(ConvGRU2D_attn, self).__init__(cell,
                                                return_sequences=return_sequences,
                                                go_backwards=go_backwards,
                                                stateful=stateful,
                                                **kwargs)
        
        self.activity_regularizer = regularizers.get(activity_regularizer)

    def call(self, inputs, mask=None, training=None, initial_state=None):
        self._maybe_reset_cell_dropout_mask(self.cell)
        
        inputs = attn_shape_adjust(inputs, self.attn_factor_reduc, reverse=False)

        if initial_state is not None:
            pass
        elif self.stateful:
            initial_state = self.states
        else:
            initial_state = self.get_initial_state(inputs) 
        
        return super(ConvGRU2D_attn, self).call(inputs,
                                            mask=mask,
                                            training=training,
                                            initial_state=initial_state) #Note: or here
    
    # region properties / config
    @property
    def filters(self):
        return self.cell.filters

    @property
    def kernel_size(self):
        return self.cell.kernel_size

    @property
    def strides(self):
        return self.cell.strides

    @property
    def padding(self):
        return self.cell.padding

    @property
    def data_format(self):
        return self.cell.data_format

    @property
    def dilation_rate(self):
        return self.cell.dilation_rate

    @property
    def activation(self):
        return self.cell.activation

    @property
    def recurrent_activation(self):
        return self.cell.recurrent_activation

    @property
    def use_bias(self):
        return self.cell.use_bias

    @property
    def kernel_initializer(self):
        return self.cell.kernel_initializer

    @property
    def recurrent_initializer(self):
        return self.cell.recurrent_initializer

    @property
    def bias_initializer(self):
        return self.cell.bias_initializer


    @property
    def kernel_regularizer(self):
        return self.cell.kernel_regularizer

    @property
    def recurrent_regularizer(self):
        return self.cell.recurrent_regularizer

    @property
    def bias_regularizer(self):
        return self.cell.bias_regularizer

    @property
    def kernel_constraint(self):
        return self.cell.kernel_constraint

    @property
    def recurrent_constraint(self):
        return self.cell.recurrent_constraint

    @property
    def bias_constraint(self):
        return self.cell.bias_constraint

    @property
    def dropout(self):
        return self.cell.dropout

    @property
    def recurrent_dropout(self):
        return self.cell.recurrent_dropout

    @property
    def implementation(self):
        return self.cell.implementation

    def get_config(self):
        config = {'filters': self.filters,
                  'kernel_size': self.kernel_size,
                  'attn_params':self.attn_params,
                  'attn_downscaling_params':self.attn_downscaling_params,
                  'attn_factor_reduc':self.attn_factor_reduc,
                  'strides': self.strides,
                  'padding': self.padding,
                  'data_format': self.data_format,
                  'dilation_rate': self.dilation_rate,
                  'activation': activations.serialize(self.activation),
                  'recurrent_activation': activations.serialize(
                      self.recurrent_activation),
                  'use_bias': self.use_bias,
                  'kernel_initializer': initializers.serialize(
                      self.kernel_initializer),
                  'recurrent_initializer': initializers.serialize(
                      self.recurrent_initializer),
                  'bias_initializer': initializers.serialize(self.bias_initializer),
                  'kernel_regularizer': regularizers.serialize(
                      self.kernel_regularizer),
                  'recurrent_regularizer': regularizers.serialize(
                      self.recurrent_regularizer),
                  'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                  'activity_regularizer': regularizers.serialize(
                      self.activity_regularizer),
                  'kernel_constraint': constraints.serialize(
                      self.kernel_constraint),
                  'recurrent_constraint': constraints.serialize(
                      self.recurrent_constraint),
                  'bias_constraint': constraints.serialize(self.bias_constraint),
                  'dropout': self.dropout,
                  'recurrent_dropout': self.recurrent_dropout,
                  'implementation':self.implementation,
                  'layer_norm':self.layer_norm}
        base_config = super(ConvGRU2D_attn, self).get_config()
        del base_config['cell']
        return dict(list(base_config.items()) + list(config.items()))
    

    @classmethod
    def from_config(cls, config):
        return cls(**config)
    # endregion 

    def get_initial_state(self, inputs):
        """inputs (samples, expanded_timesteps, rows, cols, filters)"""
        
        shape_pre_attention = K.zeros_like(inputs)
        if self.attn_ablation != 2:
            shape_post_attention = shape_pre_attention[:, :, :, :, ::self.attn_factor_reduc] #shape of output
        else:
            shape_post_attention = s

        initial_state = K.zeros_like(shape_post_attention)
            # (samples, rows, cols, filters)
        initial_state = K.sum(initial_state, axis=1)

        shape_h_state = list(self.cell.kernel_shape)
        shape_h_state[-1] = self.cell.filters

        #the issue is with initial shape
        initial_hidden_state = self.cell.input_conv(initial_state,
                                            array_ops.zeros(tuple(shape_h_state),self._compute_dtype),
                                            padding=self.cell.padding)
        
        if hasattr(self.cell.state_size, '__len__'):
            return [initial_hidden_state, initial_hidden_state ]
        else:
            return [initial_hidden_state]
        
class ConvGRU2DCell_attn(DropoutRNNCellMixin, Layer):
    """
        Cell class for the ConvGRU2D layer.
        Arguments:
            filters: Integer, the dimensionality of the output space
                (i.e. the number of output filters in the convolution).
            kernel_size: An integer or tuple/list of n integers, specifying the
                dimensions of the convolution window.
            strides: An integer or tuple/list of n integers,
                specifying the strides of the convolution.
                Specifying any stride value != 1 is incompatible with specifying
                any `dilation_rate` value != 1.
                padding: One of `"valid"` or `"same"` (case-insensitive).
                data_format: A string,
                one of `channels_last` (default) or `channels_first`.
            dilation_rate: An integer or tuple/list of n integers, specifying
                the dilation rate to use for dilated convolution.
                Currently, specifying any `dilation_rate` value != 1 is
                incompatible with specifying any `strides` value != 1.
                activation: Activation function to use.
            recurrent_activation: Activation function to use
                for the recurrent step.
            use_bias: Boolean, whether the layer uses a bias vector.
            kernel_initializer: Initializer for the `kernel` weights matrix,
                used for the linear transformation of the inputs.
            recurrent_initializer: Initializer for the `recurrent_kernel`
                weights matrix,
                used for the linear transformation of the recurrent state.
            bias_initializer: Initializer for the bias vector.
            kernel_regularizer: Regularizer function applied to
                the `kernel` weights matrix.
            recurrent_regularizer: Regularizer function applied to
                the `recurrent_kernel` weights matrix.
            bias_regularizer: Regularizer function applied to the bias vector.
            kernel_constraint: Constraint function applied to
                the `kernel` weights matrix.
            recurrent_constraint: Constraint function applied to
                the `recurrent_kernel` weights matrix.
            bias_constraint: Constraint function applied to the bias vector.
            dropout: Float between 0 and 1. Fraction of the units to drop for
                the linear transformation of the inputs.
            recurrent_dropout: Float between 0 and 1.
                Fraction of the units to drop for
                the linear transformation of the recurrent state.
        Call arguments:
            inputs: A 4D tensor.
            states:  List of state tensors corresponding to the previous timestep.
            training: Python boolean indicating whether the layer should behave in
            training mode or in inference mode. Only relevant when `dropout` or
            `recurrent_dropout` is used.
    """

    def __init__(
            self,
                filters,
                kernel_size,
                layer_norm,
                bool_ln,
                implementation,
                attn_2D,
                attn_factor_reduc,
                reset_after=False,
                strides=(1, 1),
                padding='valid',
                data_format=None,
                dilation_rate=(1, 1),
                activation='tanh',
                recurrent_activation='hard_sigmoid',
                use_bias=True,
                kernel_initializer='glorot_uniform',
                recurrent_initializer='orthogonal',
                bias_initializer='zeros',
                kernel_regularizer=None,
                recurrent_regularizer=None,
                bias_regularizer=None,
                kernel_constraint=None,
                recurrent_constraint=None,
                bias_constraint=None,
                dropout=0.,
                recurrent_dropout=0.,
                attn_ablation = 0,
                **kwargs):
        
        self.attn_ablation = attn_ablation
        self.attn_factor_reduc = attn_factor_reduc 
        super(ConvGRU2DCell_attn, self).__init__(**kwargs)
        
        self.filters = filters
        self.kernel_size = conv_utils.normalize_tuple(kernel_size, 2, 'kernel_size')
        self.strides = conv_utils.normalize_tuple(strides, 2, 'strides')
        self.padding = conv_utils.normalize_padding(padding)
        self.data_format = conv_utils.normalize_data_format(data_format)
        self.dilation_rate = conv_utils.normalize_tuple(dilation_rate, 2, 'dilation_rate')
        self.activation = activations.get(activation)
        self.recurrent_activation = activations.get(recurrent_activation)
        self.use_bias = use_bias

        self.layer_norm = layer_norm
        self.bool_ln = bool_ln
        
        self.kernel_initializer = initializers.get(kernel_initializer)
        self.recurrent_initializer = initializers.get(recurrent_initializer)
        self.bias_initializer = initializers.get(bias_initializer)

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.dropout = min(1., max(0., dropout))
        self.recurrent_dropout = min(1., max(0., recurrent_dropout))
        self.state_size = (self.filters)

        self.implementation = implementation
        self.reset_after = reset_after

        self.attn_2D = attn_2D
        
    def build(self, input_shape): 
        if self.data_format == 'channels_first':
            channel_axis = 1
        else:
            channel_axis = -1
        if input_shape[channel_axis] is None:
            raise ValueError('The channel dimension of the inputs '
                            'should be defined. Found `None`.')
        input_dim = input_shape[channel_axis]
        
        if self.attn_ablation == 2:
            kernel_shape = self.kernel_size + (input_dim*self.attn_factor_reduc, self.filters * 3)
        else:
            kernel_shape = self.kernel_size + (input_dim, self.filters * 3)
        
        self.kernel_shape = kernel_shape

        recurrent_kernel_shape = self.kernel_size + (self.filters, self.filters * 3) 
        
        self.kernel = self.add_weight(shape=kernel_shape,
                                    initializer=self.kernel_initializer,
                                    name='kernel',
                                    regularizer=self.kernel_regularizer,
                                    constraint=self.kernel_constraint)

        self.recurrent_kernel = self.add_weight(
            shape=recurrent_kernel_shape,
            initializer=self.recurrent_initializer,
            name='recurrent_kernel',
            regularizer=self.recurrent_regularizer,
            constraint=self.recurrent_constraint)

        if self.use_bias:
            bias_initializer = self.bias_initializer
            if self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3*2,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)
            elif not self.reset_after:
                self.bias = self.add_weight(
                    shape=(self.filters * 3,),
                    name='bias',
                    initializer=bias_initializer,
                    regularizer=self.bias_regularizer,
                    constraint=self.bias_constraint)
        else:
            self.bias = None
        self.built = True

    def call(self, inputs, states, training=None): #Link To Paper, Diagram
        """ConvGRU w/ Inter Layer Cross Attention Meachanism
            Note: self.reset_after==False methodology is explained in paper
            Args:
                inputs : A tensor with shape (bs, h, w, c1) #Link To Paper, Figure 1 {B_{i-1, j}}_D_{i,j}
                states : A tensor with shape (bs, sq_s ,h, w, c2)
                training : [description]. Defaults to None.


            Returns:
                [type]: A tensor with shape (bs, h, w, c3)
        """            
        
        # region --- Inter Layer Cross Attention
        h_tm1 = tf.cast( states[0], dtype=inputs.dtype)  # Link To Paper, Figure 1: B_{l, i-1}
    
        # ablation study: Cross Attention
        if self.attn_ablation == 0:
            
            inputs = attn_shape_adjust( inputs, self.attn_factor_reduc, reverse=True ) 
                        #shape (bs, self.attn_factor_reduc ,h, w, c1/self.attn_factor_reduc )
            q_antecedent = tf.expand_dims( h_tm1, axis=1)  #Link To Paper, Equation (2)
            k_antecedent = inputs                          #Link To Paper, Equation (2)
            v_antecedent = inputs                          #Link To Paper, Equation (2)
            
            attn_output = self.attn_2D( inputs=q_antecedent,
                                            k_antecedent=k_antecedent,
                                            v_antecedent=v_antecedent,
                                            training=training )
                                    #(bs, 1, h, w, f) # Link To Paper, Figure 1: \hat{B}_{l, i}
            inputs = tf.squeeze( attn_output , axis=[1])

        # ablation study: Averaging
        elif self.attn_ablation == 1:
            
            inputs = attn_shape_adjust( inputs, self.attn_factor_reduc, reverse=True ) #shape (bs, self.attn_factor_reduc ,h, w, c )
            attn_output = tf.reduce_mean( inputs, axis=1, keepdims=True ) 
            inputs = tf.squeeze( attn_output , axis=[1])

        # ablation study: Concatenation in channel
        elif self.attn_ablation == 2:
            attn_output = inputs                                                      # shape (bs, h, w, c*self.attn_factor_reduc)

        # ablation study: Using last element
        elif self.attn_ablation == 3:
            inputs = attn_shape_adjust( inputs, self.attn_factor_reduc, reverse=True ) #shape (bs, self.attn_factor_reduc ,h, w, c )
            attn_output = inputs[:, -1:, :, :, :]
            inputs = tf.squeeze( attn_output, axis=[1])
        
        # ablation study: self attention
        elif self.attn_ablation == 4:

            inputs = attn_shape_adjust( inputs, self.attn_factor_reduc, reverse=True ) #shape (bs, self.attn_factor_reduc ,h, w, c )
            q_antecedent = tf.reduce_mean(inputs, axis=1, keepdims=True)
            k_antecedent = inputs
            v_antecedent = inputs
            attn_output = self.attn_2D( inputs=q_antecedent,
                                k_antecedent=k_antecedent,
                                v_antecedent=v_antecedent,
                                training=training ) #(bs, 1, h, w, f)

            inputs = tf.squeeze( attn_output , axis=[1])
        # endregion

        # region --- 2D Conv GRU operations
        
        # Retrieving input dropout masks and recurrent dropout masks
        dp_mask = self.get_dropout_mask_for_cell(inputs, training, count=3)
        rec_dp_mask = self.get_recurrent_dropout_mask_for_cell(
                        h_tm1, training, count=3)
        
        # Retreiving bias weights
        if self.use_bias:
            if not self.reset_after:
                bias_z, bias_r, bias_h = array_ops.split(self.bias, 3)
                bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

            elif self.reset_after:
                bias_z, bias_r, bias_h, bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = array_ops.split(self.bias, 3*2)

        else:
            bias_z, bias_r, bias_h = None, None, None
            bias_z_rcrnt, bias_r_rcrnt, bias_h_rcrnt = None, None, None

        if self.implementation==1:
            
            # Applying input dropout
            if 0 < self.dropout < 1.:
                inputs_z = inputs * dp_mask[0]
                inputs_r = inputs * dp_mask[1]
                inputs_h = inputs * dp_mask[2]
            else:
                inputs_z = inputs
                inputs_r = inputs
                inputs_h = inputs

            # Retreiving input kernels
            (kernel_z, kernel_r, kernel_h) = array_ops.split(self.kernel, 3, axis=3)
            
            # Calculating input part of gates and hidden states
            x_z = self.input_conv(inputs_z, kernel_z, bias_z, padding=self.padding)
            x_r = self.input_conv(inputs_r, kernel_r, bias_r, padding=self.padding)
            x_h = self.input_conv(inputs_h, kernel_h, bias_h, padding=self.padding)
            
            # Applying reccurent dropout
            if 0 < self.recurrent_dropout < 1.:
                h_tm1_z = h_tm1 * rec_dp_mask[0]
                h_tm1_r = h_tm1 * rec_dp_mask[1]
                h_tm1_h = h_tm1 * rec_dp_mask[2]
            else:
                h_tm1_z = h_tm1
                h_tm1_r = h_tm1
                h_tm1_h = h_tm1

            # Retreiving recurrent kernel
            (recurrent_kernel_z,
                recurrent_kernel_r,
                recurrent_kernel_h) = array_ops.split(self.recurrent_kernel, 3, axis=3)
            
            # Calculating recurrent part of gates and hidden states
            recurrent_z = self.recurrent_conv(h_tm1_z, recurrent_kernel_z)
            recurrent_r = self.recurrent_conv(h_tm1_r, recurrent_kernel_r)

            if self.reset_after and self.use_bias:
                recurrent_z = K.bias_add( recurrent_z, bias_z_rcrnt )
                recurrent_r = K.bias_add( recurrent_r, bias_r_rcrnt )
            
            # Calculating gates z,r
            z = self.recurrent_activation(x_z + recurrent_z)
            r = self.recurrent_activation(x_r + recurrent_r)

            if self.reset_after:
                recurrent_h = self.recurrent_conv(h_tm1_h, recurrent_kernel_h)
                if self.use_bias:
                    recurrent_h = K.bias_add( recurrent_h, bias_h_rcrnt)
                recurrent_h = r * recurrent_h
            else:
                recurrent_h = self.recurrent_conv( r*h_tm1_h, recurrent_kernel_h )
            
            hh = self.activation( x_h + recurrent_h )

        elif self.implementation ==2:
            raise NotImplementedError
        
        if self.bool_ln:
            hh = tf.cast(self.layer_norm(hh),self._compute_dtype)
        
        h = z*h_tm1 + (1-z)*hh
        # endregion

        return h, [h]

    def input_conv(self, x, w, b=None, padding='valid'):
        conv_out = K.conv2d(x, w, strides=self.strides,
                            padding=padding,
                            data_format=self.data_format,
                            dilation_rate=self.dilation_rate)
        if b is not None:
            conv_out = K.bias_add(conv_out, b,
                                data_format=self.data_format)
        return conv_out

    def recurrent_conv(self, x, w):
        conv_out = K.conv2d(x, w, strides=(1, 1),
                            padding='same',
                            data_format=self.data_format)
        return conv_out

    def get_config(self):
        config = {'filters': self.filters,
                'kernel_size': self.kernel_size,
                'strides': self.strides,
                'padding': self.padding,
                'data_format': self.data_format,
                'dilation_rate': self.dilation_rate,
                'activation': activations.serialize(self.activation),
                'recurrent_activation': activations.serialize(
                    self.recurrent_activation),
                'use_bias': self.use_bias,
                'kernel_initializer': initializers.serialize(
                    self.kernel_initializer),
                'recurrent_initializer': initializers.serialize(
                    self.recurrent_initializer),
                'bias_initializer': initializers.serialize(self.bias_initializer),
                'kernel_regularizer': regularizers.serialize(
                    self.kernel_regularizer),
                'recurrent_regularizer': regularizers.serialize(
                    self.recurrent_regularizer),
                'bias_regularizer': regularizers.serialize(self.bias_regularizer),
                'kernel_constraint': constraints.serialize(
                    self.kernel_constraint),
                'recurrent_constraint': constraints.serialize(
                    self.recurrent_constraint),
                'bias_constraint': constraints.serialize(self.bias_constraint),
                'dropout': self.dropout,
                'recurrent_dropout': self.recurrent_dropout,
                
                'attn_2D':self.attn_2D,
                'attn_factor_reduc':self.attn_factor_reduc,
                'attn_ablation':self.attn_ablation,
                
                'implementation':self.implementation,
                'layer_norm':self.layer_norm,
                'bool_ln':self.bool_ln,
                'reset_after':self.reset_after 
                
                }
        base_config = super(ConvGRU2DCell_attn, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

#endregion

