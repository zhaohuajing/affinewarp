import numpy as np
from scipy.interpolate import interp1d
import scipy as sci
from tqdm import trange, tqdm
from .utils import modf, _fast_template_grams, quad_loss, _force_monotonic_knots
from .interp import warp_with_quadloss, densewarp, sparsewarp, sparsealign, predictwarp, warp_penalties
from numba import jit
import sparse


class AffineWarping(object):
    """Piecewise Affine Time Warping applied to an analog (dense) time series.
    """
    def __init__(self, n_knots=0, warpreg=0, l2_smoothness=0,
                 min_temp=-2, max_temp=0):

        # check inputs
        if n_knots < 0:
            raise ValueError('Number of knots must be nonnegative.')

        # model options
        self.n_knots = n_knots
        self.warpreg = warpreg
        self.l2_smoothness = l2_smoothness
        self.min_temp = min_temp
        self.max_temp = max_temp

        # initialize model now if data is provided
        self.template = None
        self.x_knots = None
        self.y_knots = None

    def _mutate_knots(self, temperature):
        x, y = self.x_knots.copy(), self.y_knots.copy()
        K, P = x.shape
        y += np.random.randn(K, P) * temperature
        if self.n_knots > 0:
            x[:, 1:-1] += np.random.randn(K, self.n_knots) * temperature
        return _force_monotonic_knots(x, y)

    def fit(self, data, **kwargs):
        """Initializes warping functions and model template and begin fitting.
        """

        # check data dimensions as input
        data = np.asarray(data)

        # check if dense array
        if data.ndim != 3 or not np.issubdtype(data.dtype, np.number):
            raise ValueError("'data' must be provided as a numpy ndarray "
                             "(neurons x timepoints x trials) holding binned "
                             "spike data.")

        # data dimensions
        K = data.shape[0]
        T = data.shape[1]
        N = data.shape[2]

        # initialize template
        self.template = data.mean(axis=0).astype(float)

        # time base
        self.tref = np.linspace(0, 1, T)

        # initialize warping functions to identity
        self.x_knots = np.tile(
            np.linspace(0, 1, self.n_knots+2),
            (K, 1)
        )
        self.y_knots = self.x_knots.copy()

        # compute initial loss
        self._losses = quad_loss(self.predict(), data)
        self._penalties = np.zeros(K)
        self.loss_hist = [np.mean(self._losses)]

        # arrays used in fit_warps function
        self._new_losses = np.empty_like(self._losses)
        self._new_penalties = np.empty_like(self._losses)

        # call fitting function
        self.continue_fit(data, **kwargs)

    def continue_fit(self, data, iterations=10, warp_iterations=20,
                     fit_template=True, verbose=True):
        """Continues optimization of warps and template (no initialization).
        """

        # check that model is initialized.
        if self.template is None:
            raise ValueError("Model not initialized. Need to call "
                             "'AffineWarping.fit(...)' before calling "
                             "'AffineWarping.continue_fit(...)'.")

        if data.shape[-1] != self.template.shape[1]:
            raise ValueError('Dimension mismatch.')

        # progress bar
        pbar = trange(iterations) if verbose else range(iterations)

        # fit model
        for it in pbar:

            # update warping functions
            self.fit_warps(data, warp_iterations)

            # user has option to only fit warps
            if fit_template:

                # update template
                self.fit_template(data)

                # update reconstruction and evaluate loss
                self._losses.fill(0.0)
                warp_with_quadloss(self.x_knots, self.y_knots, self.template,
                                   self._losses, self._losses,
                                   data, early_stop=False)

                # add warping penalty to losses
                if self.warpreg > 0:
                    self._losses += self._penalties

            # store objective function over time
            self.loss_hist.append(self._losses.mean())

            # display progress
            if verbose:
                imp = 100 * (self.loss_hist[-2] - self.loss_hist[-1]) / self.loss_hist[-2]
                pbar.set_description('Loss improvement: {0:.2f}%'.format(imp))

        return self

    def fit_warps(self, data, iterations=20, neurons=None):
        """Fit warping functions by random search.
        """

        # decay temperature within each epoch
        temperatures = np.logspace(self.min_temp, self.max_temp, iterations)

        # fit warps
        for temp in reversed(temperatures):

            # randomly sample warping functions
            X, Y = self._mutate_knots(temp)

            # recompute warping penalty
            if self.warpreg > 0:
                warp_penalties(X, Y, self._new_penalties)
                self._new_penalties *= self.warpreg
                np.copyto(self._new_losses, self._new_penalties)
            else:
                self._new_losses.fill(0.0)

            # Note: this is the bulk of computation time.
            warp_with_quadloss(X, Y, self.template, self._new_losses,
                               self._losses, data)

            # update warping parameters for trials with improved loss
            idx = self._new_losses < self._losses
            self._losses[idx] = self._new_losses[idx]
            self._penalties[idx] = self._new_penalties[idx]
            self.x_knots[idx] = X[idx]
            self.y_knots[idx] = Y[idx]

    def fit_template(self, data):
        """Fit template by least squares.
        """

        # compute normal equations
        K = data.shape[0]
        T = data.shape[1]
        N = data.shape[2]

        if self.l2_smoothness > 0:
            # coefficent matrix for the template update reduce to a
            # banded matrix with 5 diagonals.
            WtW = np.zeros((3, T))
            WtW[0, 2:] = 1.0 * self.l2_smoothness
            WtW[1, 2:] = -4.0 * self.l2_smoothness
            WtW[1, 1] = -2.0 * self.l2_smoothness
            WtW[2, 2:] = 6.0 * self.l2_smoothness
            WtW[2, 1] = 5.0 * self.l2_smoothness
            WtW[2, 0] = 1.0 * self.l2_smoothness
            _WtW = WtW[1:, :]  # makes _reduce_sum_assign target the right row.
        else:
            # coefficent matrix for the template update reduce to a
            # banded matrix with 3 diagonals.
            WtW = np.zeros((2, T))
            _WtW = WtW

        # compute gramians
        WtX = np.zeros((T, data.shape[-1]))
        _fast_template_grams(_WtW, WtX, data, self.x_knots, self.y_knots)

        # solve WtW * template = WtX
        self.template = sci.linalg.solveh_banded(WtW, WtX)

        return self.template

    def predict(self):
        # check initialization
        if self.x_knots is None:
            raise ValueError("Model not initialized. Need to call "
                             "'AffineWarping.fit(...)' before calling "
                             "'AffineWarping.predict(...)'.")

        # apply warping functions to template
        K = self.x_knots.shape[0]
        T, N = self.template.shape
        result = np.empty((K, T, N))
        return predictwarp(self.x_knots, self.y_knots, self.template, result)

    def argsort_warps(self, t=0.5):
        """
        """
        if self.x_knots is None:
            raise ValueError("Model not initialized. Need to call "
                             "'AffineWarping.fit(...)' before calling "
                             "'AffineWarping.argsort_warps(...)'.")
        if t < 0 or t > 1:
            raise ValueError('t must be between zero and one.')

        K = len(self.x_knots)
        kr = np.arange(K)
        xtst = np.full(K, t)
        y = np.empty(K)
        sparsewarp(self.x_knots, self.y_knots, kr, xtst, y)
        return np.argsort(y)

    def transform(self, X, return_array=True):
        """Apply inverse warping functions to spike data
        """

        # check initialization
        if self.x_knots is None:
            raise ValueError("Model not initialized. Need to call "
                             "'AffineWarping.fit(...)' before calling "
                             "'AffineWarping.transform(...)'.")

        # add append new axis to 2d array if necessary
        if X.ndim == 2:
            X = X[:, :, None]
        elif X.ndim != 3:
            raise ValueError('Input should be 2d or 3d array.')

        # check that first axis of X matches n_trials
        if X.shape[0] != len(self.x_knots):
            raise ValueError('Number of trials in the input does not match '
                             'the number of trials in the fitted model.')

        # length of time axis undergoing warping
        T = X.shape[1]

        # sparse array transform
        if isinstance(X, sparse.SparseArray):

            # indices of sparse entries
            trials, times, neurons = sparse.where(X)

            # find warped time
            w = sparsealign(self.x_knots, self.y_knots, trials, times / T)

            if return_array:
                # throw away out of bounds spikes
                wtimes = (w * T).astype(int)
                i = (wtimes < T) & (wtimes >= 0)
                return sparse.COO([trials[i], wtimes[i], neurons[i]],
                                  data=X.data[i], shape=X.shape)
            else:
                # return coordinates
                return (trials, w * T, neurons)

        # dense array transform
        else:
            X = np.asarray(X)
            return densewarp(self.y_knots, self.x_knots, X, np.empty_like(X))

    def dump_params(self):
        """Returns a list of model parameters for storage
        """
        return {
            'template': self.template,
            'x_knots': self.x_knots,
            'y_knots': self.y_knots,
            'loss_hist': self.loss_hist,
            'l2_smoothness': self.l2_smoothness,
            'q1': self.q1,
            'q2': self.q2
        }
