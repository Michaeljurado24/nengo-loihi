import logging

import numpy as np
from nengo.dists import Choice
from nengo.exceptions import BuildError
from nengo.neurons import (
    LIF,
    LIFRate,
    RectifiedLinear,
    RegularSpiking,
    SpikingRectifiedLinear,
)

from nengo_loihi.compat import HAS_DL, HAS_TF, nengo_dl, tf

try:
    from nengo_extras.loihi_training import (
        LoihiLIFBuilder,
        LoihiSpikingRectifiedLinearBuilder,
    )

except ImportError:  # pragma: no cover

    class ErrorBuilder:
        def __init__(self, ops):
            raise BuildError(
                "Building Loihi neuron types in nengo-dl requires nengo-extras>=0.5. "
                "Please install or upgrade nengo-extras."
            )

    LoihiLIFBuilder = LoihiSpikingRectifiedLinearBuilder = ErrorBuilder


logger = logging.getLogger(__name__)


class Installer:
    def __init__(self):
        self.installed = False

    def __call__(self):
        if self.installed or not HAS_DL:
            pass
        else:
            logger.info("Installing NengoDL neuron builders")
            nengo_dl.neuron_builders.SimNeuronsBuilder.TF_NEURON_IMPL[
                LoihiLIF
            ] = LoihiLIFBuilder
            nengo_dl.neuron_builders.SimNeuronsBuilder.TF_NEURON_IMPL[
                LoihiSpikingRectifiedLinear
            ] = LoihiSpikingRectifiedLinearBuilder
            self.installed = True


install_dl_builders = Installer()


def discretize_tau_rc(tau_rc, dt):
    """Discretize tau_rc as per discretize_compartment.

    Parameters
    ----------
    tau_rc : float
        The neuron membrane time constant.
    dt : float
        The simulator time step.
    """
    lib = tf.math if HAS_TF and isinstance(tau_rc, tf.Tensor) else np

    decay_rc = -(lib.expm1(-dt / tau_rc))
    decay_rc = lib.round(decay_rc * (2**12 - 1)) / (2**12 - 1)
    return -dt / lib.log1p(-decay_rc)


def discretize_tau_ref(tau_ref, dt):
    """Discretize tau_ref as per Compartment.configure_lif.

    Parameters
    ----------
    tau_rc : float
        The neuron membrane time constant.
    dt : float
        The simulator time step.
    """
    lib = tf.math if HAS_TF and isinstance(tau_ref, tf.Tensor) else np

    return dt * lib.round(tau_ref / dt)


def loihi_lif_rates(neuron_type, x, gain, bias, dt, amplitude=None):
    tau_ref = discretize_tau_ref(neuron_type.tau_ref, dt)
    tau_rc = discretize_tau_rc(neuron_type.tau_rc, dt)
    amplitude = neuron_type.amplitude if amplitude is None else amplitude

    j = neuron_type.current(x, gain, bias) - 1
    out = np.zeros_like(j)
    period = tau_ref + tau_rc * np.log1p(1.0 / j[j > 0])
    out[j > 0] = (amplitude / dt) / np.ceil(period / dt)
    return out


def loihi_spikingrectifiedlinear_rates(neuron_type, x, gain, bias, dt, amplitude=None):
    amplitude = neuron_type.amplitude if amplitude is None else amplitude

    j = neuron_type.current(x, gain, bias)
    out = np.zeros_like(j)
    period = 1.0 / j[j > 0]
    out[j > 0] = (amplitude / dt) / np.ceil(period / dt)
    return out


def loihi_regularspiking_rates(neuron_type, x, gain, bias, dt):
    base_type = neuron_type.base_type
    if type(base_type) is LIFRate:
        return loihi_lif_rates(
            base_type, x, gain, bias, dt, amplitude=neuron_type.amplitude
        )
    elif type(base_type) is RectifiedLinear:
        return loihi_spikingrectifiedlinear_rates(
            base_type, x, gain, bias, dt, amplitude=neuron_type.amplitude
        )
    else:
        return neuron_type.rates(x, gain, bias)


def _broadcast_rates_inputs(x, gain, bias):
    x = np.array(x, dtype=float, copy=False, ndmin=1)
    gain = np.array(gain, dtype=float, copy=False, ndmin=1)
    bias = np.array(bias, dtype=float, copy=False, ndmin=1)
    if x.ndim == 1:
        x = x[:, np.newaxis] * np.ones(gain.shape[-1])
    return x, gain, bias


def loihi_rates(neuron_type, x, gain, bias, dt):
    x, gain, bias = _broadcast_rates_inputs(x, gain, bias)
    for cls in type(neuron_type).__mro__:
        if cls in loihi_rate_functions:
            return loihi_rate_functions[cls](neuron_type, x, gain, bias, dt)
    return neuron_type.rates(x, gain, bias)


def nengo_rates(neuron_type, x, gain, bias):
    """Call NeuronType.rates with Nengo 3.0 broadcasting rules"""
    x, gain, bias = _broadcast_rates_inputs(x, gain, bias)
    return neuron_type.rates(x, gain, bias)


loihi_rate_functions = {
    LIF: loihi_lif_rates,
    SpikingRectifiedLinear: loihi_spikingrectifiedlinear_rates,
    RegularSpiking: loihi_regularspiking_rates,
}


class LoihiLIF(LIF):
    """Simulate LIF neurons as done by Loihi.

    On Loihi, the inter-spike interval has to be an integer. This causes
    aliasing the firing rates where a wide variety of inputs can produce the
    same output firing rate. This class reproduces this effect, as well as
    the discretization of some of the neuron parameters. It can be used in
    e.g. ``nengo`` or ``nengo_dl`` to reproduce these unique Loihi effects.

    Parameters
    ----------
    nengo_dl_noise : `nengo_extras.loihi_training.NeuronOutputNoise`
        Noise added to the rate-neuron output when training with this neuron
        type in ``nengo_dl``.
    """

    state = {
        "voltage": Choice([0]),
        "refractory_time": Choice([0]),
    }

    def __init__(
        self,
        tau_rc=0.02,
        tau_ref=0.002,
        min_voltage=0,
        amplitude=1,
        nengo_dl_noise=None,
        **kwargs,
    ):
        super().__init__(
            tau_rc=tau_rc,
            tau_ref=tau_ref,
            min_voltage=min_voltage,
            amplitude=amplitude,
            **kwargs,
        )
        self.nengo_dl_noise = nengo_dl_noise
        install_dl_builders()

    @property
    def _argreprs(self):
        args = super()._argreprs
        if self.nengo_dl_noise is not None:
            args.append("nengo_dl_noise=%s" % self.nengo_dl_noise)
        return args

    def rates(self, x, gain, bias, dt=0.001):
        return loihi_lif_rates(self, x, gain, bias, dt)

    def step(self, dt, J, output, voltage, refractory_time):
        tau_ref = discretize_tau_ref(self.tau_ref, dt)
        tau_rc = discretize_tau_rc(self.tau_rc, dt)

        refractory_time -= dt
        delta_t = (dt - refractory_time).clip(0, dt)
        voltage -= (J - voltage) * np.expm1(-delta_t / tau_rc)

        spikes_mask = voltage > 1
        output[:] = spikes_mask * (self.amplitude / dt)

        voltage[voltage < self.min_voltage] = self.min_voltage
        voltage[spikes_mask] = 0
        refractory_time[spikes_mask] = tau_ref + dt


class LoihiSpikingRectifiedLinear(SpikingRectifiedLinear):
    """Simulate spiking rectified linear neurons as done by Loihi.

    On Loihi, the inter-spike interval has to be an integer. This causes
    aliasing in the firing rates such that a wide variety of inputs produce the
    same output firing rate. This class reproduces this effect. It can be used
    in e.g. ``nengo`` or ``nengo_dl`` to reproduce these unique Loihi effects.
    """

    state = {
        "voltage": Choice([0]),
    }

    def __init__(self, amplitude=1, **kwargs):
        super().__init__(amplitude=amplitude, **kwargs)
        install_dl_builders()

    def rates(self, x, gain, bias, dt=0.001):
        return loihi_spikingrectifiedlinear_rates(self, x, gain, bias, dt)

    def step(self, dt, J, output, voltage):
        voltage += J * dt

        spikes_mask = voltage > 1
        output[:] = spikes_mask * (self.amplitude / dt)

        voltage[voltage < 0] = 0
        voltage[spikes_mask] = 0
