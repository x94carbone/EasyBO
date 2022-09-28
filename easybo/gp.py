"""Wrappers for the gpytorch and botorch libraries. See
`here <https://botorch.org/docs/models>`__ for important details about the
type of models that we wrap.

Gaussian Processes can often be difficult to get working the first time a new
user tries them, e.g. ambiguities in choosing the kernels. The classes here
abstract away that difficulty (and others) by default.
"""

from copy import deepcopy

import botorch
from botorch.models import (
    SingleTaskGP,
    FixedNoiseGP,
    HeteroskedasticSingleTaskGP,
)
import gpytorch
from gpytorch.constraints import GreaterThan
import numpy as np
import torch

from easybo.utils import _to_float32_tensor, _to_long_tensor, DEVICE
from easybo.logger import logger


class EasyGP:
    """Core base class for defining all the primary operations required for an
    "easy Gaussian Process"."""

    def transform_x(self, x):
        """Executes a forward transformation of some sort on the input data.
        This defaults to a simple conversion to a float32 tensor and placing
        that object on the correct device.

        Parameters
        ----------
        x : array_like

        Returns
        -------
        torch.tensor
        """

        return _to_float32_tensor(x, device=self.device)

    def transform_y(self, y):
        """Executes a forward transformation of some sort on the output data.
        This defaults to a simple conversion to a float32 tensor and placing
        that object on the correct device.

        Parameters
        ----------
        y : array_like

        Returns
        -------
        torch.tensor
        """

        return _to_float32_tensor(y, device=self.device)

    @property
    def device(self):
        """The device on which to run the calculations and place the model.
        Follows the PyTorch standard, e.g. "cpu", "gpu:0", etc.

        Returns
        -------
        str
        """

        return self._device

    @device.setter
    def device(self, device):
        """Sets the device. This not only changes the device attribute, it will
        send the model to the new device.

        Parameters
        ----------
        device : str
        """

        self._model.to(device)
        self._device = device

    @property
    def likelihood(self):
        """The likelihood function mapping the values f(x) to the observations
        y. See `here <https://docs.gpytorch.ai/en/latest/likelihoods.html>`__
        for more details.

        Returns
        -------
        TYPE
            Description
        """
        return self._model.likelihood

    @property
    def model(self):
        """Returns the GPyTorch model itself.

        Returns
        -------
        botorch.models
        """

        return self._model

    def _get_current_train_x(self):
        return self._model.train_inputs[0]

    @property
    def train_x(self):
        """The training inputs. Should be of shape ``N_train x d_in``.

        Returns
        -------
        numpy.ndarray
        """

        return self._get_current_train_x().detach().numpy()

    def _get_current_train_y(self):
        return self._model.train_targets.reshape(-1, 1)

    @property
    def train_y(self):
        """The training targets. Should be of shape ``N_train x d_out``. Note
        that for classification, these should be one-hot encoded, e.g.
        ``np.array([0, 1, 2, 1, 2, 0, 0])``.

        Returns
        -------
        numpy.ndarray
        """

        return self._get_current_train_y().detach().numpy()

    def _fit_model_(
        self,
        *,
        model,
        optimizer,
        optimizer_kwargs,
        training_iter,
        print_frequency,
        heteroskedastic_training=False,
    ):

        model.train()
        _optimizer = optimizer(model.parameters(), **optimizer_kwargs)
        mll = gpytorch.mlls.ExactMarginalLogLikelihood(
            likelihood=model.likelihood, model=model
        )
        train_x = self._get_current_train_x()
        train_y = self._get_current_train_y()
        mll.to(train_x)

        # Standard training loop...
        losses = []
        for ii in range(training_iter + 1):

            _optimizer.zero_grad()
            output = model(train_x)

            if heteroskedastic_training:
                loss = -mll(output, train_y, train_x).sum()
            else:
                loss = -mll(output, train_y).sum()
            loss.backward()
            _loss = loss.item()
            ls = model.covar_module.base_kernel.lengthscale.mean().item()
            try:
                noise = model.likelihood.noise.mean().item()
            except AttributeError:
                noise = 0.0
            msg = (
                f"{ii}/{training_iter} loss={_loss:.03f} lengthscale="
                f"{ls:.03f} noise={noise:.03f}"
            )
            if print_frequency != 0:
                if ii % (training_iter // print_frequency) == 0:
                    logger.info(msg)
            logger.debug(msg)

            _optimizer.step()
            losses.append(loss.item())

        return losses, mll

    def train_(
        self,
        *,
        optimizer=torch.optim.Adam,
        optimizer_kwargs={"lr": 0.1},
        training_iter=100,
        print_frequency=5,
    ):
        """Trains the provided botorch model. The methods used here are
        different than botorch's boilerplate ``fit_gpytorch_model`` and allow
        for a bit more functionality (which is also documented in some of
        BoTorch's tutorials).

        Parameters
        ----------
        optimizer : torch.optim, optional
            The optimizer to use to train the GP.
        optimizer_kwargs : dict, optional
            Keyword arguments to pass to the optimizer.
        training_iter : int, optional
            The number of training iterations to perform.
        print_frequency : int, optional
            The frequency at which to log to the info logger during training.
            If 0 does not print anything during training.

        Returns
        -------
        list
            A list of the losses as a function of epoch aka ``training_iter``.
        """

        return self._fit_model_(
            model=self.model,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            training_iter=training_iter,
            print_frequency=print_frequency,
            heteroskedastic_training=False,
        )

    def predict(self, *, grid, parsed=True, use_likelihood=True):
        """Runs inference on the model in eval mode.

        Parameters
        ----------
        grid : array_like
            The grid on which to perform inference.
        parsed : bool, optional
            If True, returns a dictionary with the keys "mean",
            "mean-2sigma" and "mean+2sigma", representing the mean prediction
            of the posterior, as well as the mean +/- 2sigma, in addition to
            the ``gpytorch.distributions.MultivariateNormal`` object. If False,
            returns the full ``gpytorch.distributions.MultivariateNormal``
            only.
        use_likelihood : bool, optional
            If True, applies the likelihood forward operation to the model
            forward operation. This is the recommended default behavior.
            Otherwise, just uses the model forward behavior without accounting
            for the likelihood.

        Returns
        -------
        dict or gpytorch.distributions.MultivariateNormal
        """

        grid = _to_float32_tensor(grid, device=self.device)

        self.model.eval()

        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            if use_likelihood:
                observed_pred = self.likelihood(self.model(grid))
            else:
                observed_pred = self.model(grid)

        if parsed:
            lower, upper = observed_pred.confidence_region()
            return {
                "mean": observed_pred.mean.detach().numpy(),
                "mean-2sigma": lower.detach().numpy(),
                "mean+2sigma": upper.detach().numpy(),
                "observed_pred": observed_pred,
            }
        return observed_pred

    def sample(self, *, grid, samples=10, seed=None):
        """Samples from the provided model.

        Parameters
        ----------
        grid : array_like
            The grid from which to sample.
        samples : int, optional
            Number of samples to draw.
        seed : None, optional
            Seeds the random number generator via ``torch.manual_seed``.

        Returns
        -------
        numpy.array
            The array of sampled data, of shape ``samples x len(grid)``.
        """

        if seed is not None:
            torch.manual_seed(seed)
        grid = _to_float32_tensor(grid, device=self.device)
        result = self.model(grid).sample(sample_shape=torch.Size([samples]))
        return result.detach().numpy()

    def sample_reproducibly(self, *, grid, seed):
        """TODO

        Parameters
        ----------
        grid : TYPE
            Description
        seed : TYPE
            Description

        Returns
        -------
        TYPE
            Description
        """

        return np.array(
            [
                self.sample(
                    grid=np.array([xx]),
                    samples=1,
                    seed=seed,
                )
                for xx in grid
            ]
        ).squeeze()

    def tell_(self, *, new_x, new_y):
        """Informs the GP about new data. This implicitly conditions the model
        on the new data but without modifying the previous model's
        hyperparameters.

        .. warning::

            The input shapes of the new x and y values must be correct
            otherwise errors will be thrown.

        Parameters
        ----------
        new_x : array_like
            The new input data.
        new_y : array_like
            The new target data.
        """

        new_x = self.transform_x(new_x)
        new_y = self.transform_y(new_y)
        self._model = self._model.condition_on_observations(new_x, new_y)


class EasySingleTaskGPRegressor(EasyGP):
    def __init__(
        self,
        *,
        train_x,
        train_y,
        likelihood=gpytorch.likelihoods.GaussianLikelihood(),
        mean_module=gpytorch.means.ConstantMean(),
        covar_module=gpytorch.kernels.ScaleKernel(
            gpytorch.kernels.RBFKernel()
        ),
        device=DEVICE,
        **kwargs,
    ):
        self._device = device
        model = SingleTaskGP(
            train_X=self.transform_x(train_x),
            train_Y=self.transform_y(train_y),
            likelihood=likelihood,
            mean_module=mean_module,
            covar_module=covar_module,
            **kwargs,
        )
        self._model = deepcopy(model.to(device))


class EasyFixedNoiseGPRegressor(EasyGP):
    def __init__(
        self,
        *,
        train_x,
        train_y,
        train_yvar,
        mean_module=gpytorch.means.ConstantMean(),
        covar_module=gpytorch.kernels.ScaleKernel(
            gpytorch.kernels.RBFKernel()
        ),
        device=DEVICE,
        **kwargs,
    ):
        self._device = device
        model = FixedNoiseGP(
            train_X=self.transform_x(train_x),
            train_Y=self.transform_y(train_y),
            train_Yvar=self.transform_y(train_yvar),
            mean_module=mean_module,
            covar_module=covar_module,
            **kwargs,
        )
        self._model = deepcopy(model.to(device))


class MostLikelyHeteroskedasticGPRegressor(EasyGP):
    def __init__(
        self,
        *,
        train_x,
        train_y,
        likelihood=gpytorch.likelihoods.GaussianLikelihood(),
        mean_module=gpytorch.means.ConstantMean(),
        covar_module=gpytorch.kernels.ScaleKernel(
            gpytorch.kernels.RBFKernel()
        ),
        device=DEVICE,
        **kwargs,
    ):
        self._device = device

        # Define the standard single task GP model
        model = SingleTaskGP(
            train_X=self.transform_x(train_x),
            train_Y=self.transform_y(train_y),
            likelihood=likelihood,
            mean_module=mean_module,
            covar_module=covar_module,
            **kwargs,
        )
        model.likelihood.noise_covar.register_constraint(
            "raw_noise", GreaterThan(1e-3)
        )
        self._model = deepcopy(model.to(device))

        # Define a noise model - this is going to be initialized during
        # training
        self._noise_model = None

    def train_(
        self,
        *,
        optimizer=torch.optim.Adam,
        optimizer_kwargs={"lr": 0.1},
        training_iter=100,
        print_frequency=5,
    ):

        # homoskedastic_loss = self._fit_model_(
        #     model=self.model,
        #     optimizer=optimizer,
        #     optimizer_kwargs=optimizer_kwargs,
        #     training_iter=training_iter,
        #     print_frequency=print_frequency,
        #     heteroskedastic_training=False
        # )

        mll = gpytorch.mlls.ExactMarginalLogLikelihood(
            likelihood=self.model.likelihood, model=self.model
        )
        botorch.fit.fit_gpytorch_model(mll)

        # Now we have to fit the noise model; first we get the observed
        # variance
        self.model.eval()
        c_train_x = self._get_current_train_x()
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            post = self.model.posterior(c_train_x).mean.numpy()
        observed_var = torch.tensor(
            (post - self.train_y) ** 2, dtype=torch.float
        )

        # Now actually fit the noise model
        self._noise_model = HeteroskedasticSingleTaskGP(
            train_X=self._get_current_train_x(),
            train_Y=self._get_current_train_y(),
            train_Yvar=observed_var,
        )
        # heteroskedastic_loss = self._fit_model_(
        #     model=self._noise_model,
        #     optimizer=optimizer,
        #     optimizer_kwargs=optimizer_kwargs,
        #     training_iter=training_iter,
        #     print_frequency=print_frequency,
        #     heteroskedastic_training=True
        # )

        mll2 = gpytorch.mlls.ExactMarginalLogLikelihood(
            likelihood=self._noise_model.likelihood, model=self._noise_model
        )
        botorch.fit.fit_gpytorch_model(mll2, max_retries=10)

        self.model.train()

        # return heteroskedastic_loss


class EasySingleTaskGPClassifier(EasyGP):
    def transform_y(self, y):
        """Executes a forward transformation of some sort on the output data.
        For the classifier, this is a conversion to a long tensor.

        Parameters
        ----------
        y : array_like

        Returns
        -------
        torch.tensor
        """

        return _to_long_tensor(y, device=self.device)

    def __init__(
        self,
        *,
        train_x,
        train_y,
        mean_module=gpytorch.means.ConstantMean(),
        covar_module=gpytorch.kernels.ScaleKernel(
            gpytorch.kernels.RBFKernel()
        ),
        device=DEVICE,
        **kwargs,
    ):
        self._device = device
        y = self.transform_y(train_y)
        lh = gpytorch.likelihoods.DirichletClassificationLikelihood(
            y, learn_additional_noise=True
        )
        model = SingleTaskGP(
            train_X=self.transform_x(train_x),
            train_Y=y,
            likelihood=lh,
            mean_module=mean_module,
            covar_module=covar_module,
            **kwargs,
        )
        self._model = deepcopy(model.to(device))
