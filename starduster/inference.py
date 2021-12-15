import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from tqdm import tqdm


class ErrorFunction(nn.Module):
    """Base class for error functions.

    param_names : list
        Free parameter names of the error functions. If None, assume no free
        parameter.
    bounds : array
        An array of (min, max) to specify the bounds of the free parameters.
    """
    def __init__(self, param_names=None, bounds=None):
        super().__init__()
        if param_names is None:
            self.n_params = 0
            self.param_names = []
            self.bounds = np.empty((0, 2), dtype=np.float64)
        else:
            self.n_params = len(param_names)
            self.param_names = param_names
            self.bounds = np.atleast_2d(bounds)
            lbounds, ubounds = torch.tensor(bounds, dtype=torch.float32).T
            self.register_buffer('lbounds', lbounds)
            self.register_buffer('ubounds', ubounds)


    def check_bounds(self, params):
        """Check if any parameters are beyond the bounds.

        Returns
        -------
        bool
            True if any parameters are beyond the bounds.
        """
        assert self.n_params > 0
        return torch.any(params <= self.lbounds, dim=-1) \
            | torch.any(params >= self.ubounds, dim=-1)


class Gaussian(ErrorFunction):
    """Gaussian error function.

    Return the logarithmic Gaussian errors.

    Parameters
    ----------
    y_obs : tensor
        Mean of the normal distribution.
    y_err : tensor
        Standard deviation of the normal distribution.
    norm : bool
        If False, only return the value in the exponent; otherwise add the
        normalisation factor to the output.

    Inputs
    ------
    y_pred : tensor
        (N, M), where M is the number of the observational data.

    Outputs
    -------
        (N,).
    """
    def __init__(self, y_obs, y_err, norm=True):
        super().__init__()
        self.register_buffer('y_obs', y_obs)
        self.register_buffer('y_err', y_err)
        if norm:
            self.norm = torch.sum(-torch.log(np.sqrt(2*np.pi)*y_err))
        else:
            self.norm = 0.


    def forward(self, *args):
        delta = (args[0] - self.y_obs)/self.y_err
        return torch.sum(-.5*delta*delta, dim=-1) + self.norm


class GaussianWithScatter(ErrorFunction):
    """Gaussian error function with a single intrinsic scatter.

    Return the logarithmic Gaussian errors.

    Parameters
    ----------
    y_obs : tensor
        Mean of the normal distribution.
    bounds : tuple
        An tuple of (min, max) to specify the bounds of the intrinsic scatter.
        The bounds should be in base 10 logarithmic scale.

    Inputs
    ------
    y_pred : tensor
        (N, M), where M is the number of the observational data.

    Outputs
    -------
        (N,).
    """
    def __init__(self, y_obs, bounds=(-2., 0.)):
        super().__init__(['sigma'], bounds)
        self.register_buffer('y_obs', y_obs)


    def forward(self, *args):
        y_pred, log_sigma = args
        sigma = 10**log_sigma
        delta = (y_pred - self.y_obs)/sigma
        return torch.sum(-.5*delta*delta, dim=-1) \
            - self.y_obs.size(0)*torch.ravel(torch.log(np.sqrt(2*np.pi)*sigma))


class Posterior(nn.Module):
    """A posterior distribution that can be passed to various optimisation and
    sampling tools.

    Parameters
    ----------
    sed_model : MultiwavelengthSED
        SED model.
    error_func : ErrorFunction
        Error function.
    """
    def __init__(self, sed_model):
        super().__init__()
        self.sed_model = sed_model
        self.configure_output_mode()


    def forward(self, params):
        model_input_size = self.sed_model.input_size
        if self._output_mode == 'numpy_grad':
            params = torch.tensor(
                params, dtype=torch.float32, requires_grad=True
            )
            p_model = params[:model_input_size]
            p_error = params[model_input_size:]
        else:
            params = torch.as_tensor(
                params, dtype=torch.float32, device=self.sed_model.adapter.device
            )
            params = torch.atleast_2d(params)
            p_model = params[:, :model_input_size]
            p_error = params[:, model_input_size:]

        y_pred, is_out = self.sed_model(p_model, return_ph=True, check_bounds=True)
        if self.error_func.n_params > 0:
            is_out |= self.error_func.check_bounds(p_error)
        log_post = self._sign*(self.error_func(y_pred, p_error) + self.log_out*is_out)

        if self._output_mode == 'numpy':
            return np.squeeze(log_post.detach().cpu().numpy())
        elif self._output_mode == 'numpy_grad':
            log_post.backward()
            return log_post.detach().cpu().numpy(), np.array(params.grad.cpu(), dtype=np.float64)
        return log_post


    def configure_output_mode(self, output_mode='torch', negative=False, log_out=-1e15):
        """Configure the output mode.

        Parameters
        ----------
        output_mode : string {'torch', 'numpy', 'numpy_grad'}
            'torch' : Return a PyTorch tensor.
            'numpy' : Return a numpy array.
            'numpy_grad' : Return a tuple with the second element be the
            gradient.

        negative : bool
            If True, multiply the output by -1.
        log_out : float
            Add this value to the output if the input is beyond the effective
            region.
        """
        if output_mode in ['torch', 'numpy', 'numpy_grad']:
            self._output_mode = output_mode
        else:
            raise ValueError(f"Unknown output mode: {output_mode}.")
        if negative:
            self._sign = -1.
        else:
            self._sign = 1.
        self.log_out = log_out


    def save_inference_state(self, fname, data):
        config_adapter = self.sed_model.adapter._get_config()
        config_detector = self.sed_model.detector._get_config()
        inference_state = InferenceState(self.error_func, config_adapter, config_detector, data)
        torch.save(inference_state, fname)


    def load_inference_state(self, target):
        if isinstance(target, InferenceState):
            inference_state = target
        else:
            inference_state = torch.load(target, self.sed_model.adapter.device)
        self.error_func = inference_state.error_func
        config_adapter, config_detector = inference_state.get_config()
        self.sed_model.configure_input_mode(**config_adapter)
        self.sed_model.configure_output_mode(**config_detector)
        return inference_state.data


    @property
    def input_size(self):
        """Number of input parameters."""
        return self.sed_model.adapter.input_size + self.error_func.n_params


    @property
    def param_names(self):
        """Parameter names"""
        return self.sed_model.adapter.param_names + self.error_func.param_names


    @property
    def bounds(self):
        """Bounds of input parameters."""
        return np.vstack([self.sed_model.adapter.bounds, self.error_func.bounds])


class InferenceState(nn.Module):
    def __init__(self, error_func, config_adapter=None, config_detector=None, data=None):
        super().__init__()
        self.error_func = error_func
        self.data = data
        if config_adapter is not None:
            for key, val in config_adapter.items():
                setattr(self, 'adapter.' + key, val)
        if config_detector is not None:
            for key, val in config_detector.items():
                setattr(self, 'detector.' + key, val)


    def get_config(self):
        config_adapter = {}
        config_detector = {}
        for name in dir(self):
            if 'adapter' in name:
                config_adapter[name.replace('adapter.', '')] = getattr(self, name)
            if 'detector' in name:
                config_detector[name.replace('detector.', '')] = getattr(self, name)
        return config_adapter, config_detector


class OptimizerWrapper(nn.Module):
    """A wrapper that allows a posterior distribution to be minimised by
    PyTorch optimizers.

    Parameters
    ----------
    log_post : Posterior
        Target posterior distribution.
    x0 : tensor
        Initial parameters.

    Output
    ------
        Scalar.
    """
    def __init__(self, log_post, x0):
        super().__init__()
        self.params = nn.Parameter(x0)
        # Save log_post as a tuple to prevent addtional parameters
        self._log_post = log_post,


    def forward(self):
        return self._log_post[0](self.params)


def optimize(log_post, cls_opt, x0=None, n_step=1000, lr=1e-2, progress_bar=True, **kwargs_opt):
    """Optimise a posterior distribution using a Pytorch optimiser.

    Parameters
    ----------
    log_post : Posterior
        Target posterior distribution.
    cls_opt : torch.optim.Optimizer
        PyTorch optimiser class.
    x0 : tensor
        Initial parameters.
    n_step : int
        Number of the optimisation steps.
    lr : float
        Learning rate.
    progress_bar : bool
        If True, show a progress bar.
    **kwargs_opt
        Keyword arguments used to initalise the optimiser.


    Returns
    -------
    tensor
        Best-fitting parameters.
    """
    model = OptimizerWrapper(log_post, x0)
    opt = cls_opt(model.parameters(), lr=lr, **kwargs_opt)
    with tqdm(total=n_step, disable=(not progress_bar)) as pbar:
        for i_step in range(n_step):
            loss = model()
            loss.backward()
            opt.step()
            opt.zero_grad()

            pbar.set_description('loss: %.3e'%float(loss))
            pbar.update()

    return model.params.detach()

