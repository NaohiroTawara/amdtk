"""
Main class of the phone loop model.

Copyright (C) 2017, Lucas Ondel

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""

import numpy as np
from scipy.special import logsumexp
from .efd import EFDStats
from .svae_prior import SVAEPrior
from .sga_training import StdSGAPython, AdamSGAPython


class PhoneLoop(SVAEPrior, StdSGAPython, AdamSGAPython):
    """Bayesian Phone Loop.

    Bayesian Phone Loop with a Dirichlet prior over the weights.

    """

    @staticmethod
    def __log_transition_matrix(n_units, n_states, mixture_weights,
                                prob_final_state=.5):
        """Create a log transition matrix."""
        tot_n_states = n_units * n_states
        init_states = np.arange(0, tot_n_states, n_states)
        final_states = init_states + n_states - 1
        trans_mat = np.zeros((tot_n_states, tot_n_states)) + float('-inf')

        for idx, init_state in enumerate(init_states):
            for offset in range(n_states - 1):
                state = init_state + offset
                trans_mat[state, state: state + n_states - 1] = np.log(.5)
            if n_states > 1:
                trans_mat[final_states[idx], final_states[idx]] = \
                    np.log(prob_final_state)

        for final_state in final_states:
            if n_states > 1:
                trans_mat[final_state, init_states] = \
                    np.log((1 - prob_final_state) * mixture_weights)
            else:
                trans_mat[final_state, init_states] = np.log(mixture_weights)

        return trans_mat, init_states, final_states

    def __init__(self, prior, posterior, components):
        """Initialize the Phone Loop.

        Parameters
        ----------
        prior : :class:`Dirichlet`
            Dirichlet prior of the mixture.
        posterior : :class:`Dirichlet`
            Dirichlet posterior of the mixture.
        compents : list
            List of :class:`Normal`

        """
        self.prior = prior
        self.posterior = posterior
        self.components = components

        # Matrix of the components' parameters.
        n_comp = len(components)
        n_params = len(components[0].prior.natural_params)
        self.comp_params = np.zeros((n_comp, n_params))

        self.n_units = len(prior.natural_params)
        self.n_states = int(len(components) / self.n_units)

        # Expected value of the units' weights.
        weights = self.posterior.grad_log_partition

        self.log_trans_mat, self.init_states, self.final_states = \
            PhoneLoop.__log_transition_matrix(self.n_units, self.n_states,
                                              weights)

        self._prepare()

        # List of parameters to update.
        self.params = [posterior.natural_params]
        for component in self.components:
            self.params.append(component.posterior.natural_params)

        StdSGAPython.__init__(self)
        AdamSGAPython.__init__(self)

    def _prepare(self):
        # Update the expected value of the parameters of the Gaussian
        # components.
        values = np.vstack(
            [comp.posterior.grad_log_partition for idx, comp in
             enumerate(self.components)]
        )
        self.comp_params = values

        # Update the log transition matrix.
        exp_log_weights = self.posterior.grad_log_partition
        prob_fs = np.exp(self.log_trans_mat[self.final_states[0],
                                            self.final_states[0]])
        for final_state in self.final_states:
            if self.n_states > 1:
                self.log_trans_mat[final_state, self.init_states] = \
                    (np.log((1 - prob_fs)) + exp_log_weights)
            else:
                self.log_trans_mat[final_state, self.init_states] = \
                    exp_log_weights

    def get_sufficient_stats(self, data):
        """Sufficient statistics of GMM.

        The sufficient statistics of the GMM include the ones of the
        mixture's component appended with a '1' (for the Dirichlet
        prior).

        Parameters
        ----------
        data : numpy.ndarray
            (NxD) matrix where N is the number of frames and D is the
            dimension of a single features vector.

        Returns
        -------
        s_stats : numpy.ndarray
            (NxD)2 matrix of sufficient statistics. D2 is the dimension
            of the sufficient statistics for a single features frame.

        """
        cls = self.components[0].__class__
        return cls.get_sufficient_stats(data)

    def forward(self, llhs):
        """Forward recursion.

        Parameters
        ----------
        llhs : numpy.ndarray
            Log of the emission probabilites with shape (N x K) where N
            is the number of frame in the sequence and K is the number
            of state in the HMM model.

        Returns
        -------
        log_alphas : numpy.ndarray
            The log alphas values of the recursions.

        """
        log_init = self.posterior.grad_log_partition
        log_init -= logsumexp(log_init)
        log_alphas = np.zeros_like(llhs) - np.inf
        log_alphas[0, self.init_states] = llhs[0, self.init_states] + log_init
        log_A = self.log_trans_mat
        for i in range(1, llhs.shape[0]):
            log_alphas[i] = llhs[i]
            log_alphas[i] += logsumexp(log_alphas[i-1] + log_A.T, axis=1)
        return log_alphas

    def backward(self, llhs):
        """Backward recursion.

        Parameters
        ----------
        llhs : numpy.ndarray
            Log of the emission probabilites with shape (N x K) where N
            is the number of frame in the sequence and K is the number
            of state in the HMM model.

        Returns
        -------
        log_alphas : numpy.ndarray
            The log alphas values of the recursions.

        """
        log_A = self.log_trans_mat
        log_betas = np.zeros_like(llhs) - np.inf
        log_betas[-1, self.final_states] = 0.
        for i in reversed(range(llhs.shape[0]-1)):
            log_betas[i] = logsumexp(log_A + llhs[i+1] + log_betas[i+1],
                                     axis=1)
        return log_betas

    def units_stats(self, c_llhs, log_alphas, log_betas):
        """Extract the statistics needed to re-estimate the
        weights of the units.
        Parameters
        ----------
        c_llhs : numpy.ndarray
            Emissions log-likelihood.
        log_alphas : numpy.ndarray
            Log of the results of the forward recursion.
        log_betas : numpy.ndarray
            Log of the results of the backward recursion.
        Returns
        -------
        units_stats : numpy.ndarray
            Units' statistics.
        """
        log_units_stats = np.zeros(self.n_units)
        norm = logsumexp(log_alphas[-1] + log_betas[-1])

        for n_unit in range(self.n_units):
            index1 = n_unit * self.n_states + 1
            index2 = index1 + 1
            log_prob_trans = self.log_trans_mat[index1, index2]
            log_q_zn1_zn2 = log_alphas[:-1, index1] + c_llhs[1:, index2] + \
                log_prob_trans + log_betas[1:, index2]
            log_q_zn1_zn2 -= norm
            log_units_stats[n_unit] = logsumexp(log_q_zn1_zn2)

        return np.exp(log_units_stats)

    def update_posterior(self, acc_s_stats):
        """Update the posterior distribution.

        Parameters
        ----------
        acc_s_stats : numpy.ndarray
            Accumulated sufficient statistics.

        """
        for idx, stats in enumerate(acc_s_stats[:, :-1]):
            self.components[idx].update_posterior(stats)
        self.posterior.natural_params = self.prior.natural_params + \
            acc_s_stats[:, -1]

    def kl_div_posterior_prior(self):
        """Kullback-Leibler divergence between prior /posterior.

        Returns
        -------
        kl_div : float
            Kullback-Leibler divergence.

        """
        retval = 0.
        retval += self.posterior.kl_div(self.prior)
        for comp in self.components:
            retval += comp.posterior.kl_div(comp.prior)
        return retval

    # SVAEPrior interface.
    # ------------------------------------------------------------------

    def init_resps(self, n_frames):
        """Get the initialize per-frame responsibilities.

        Parameters
        ----------
        n_frames : numpy.ndarray,
            Number of frames for the mini-batch.

        Returns
        -------
        resps : numpy.ndarray
            Initial per-frame responsibilities.

        """
        n_units = self.n_units
        n_states = self.n_states
        prob = np.exp(self.posterior.grad_log_partition)
        prob /= prob.sum()
        tmp = np.ones((n_units, n_states)) * (1 / n_states)
        prob = (tmp * prob[:, np.newaxis]).reshape(n_units * n_states)
        return np.ones((n_frames, n_units * n_states)) * prob

    def get_resps(self, s_stats, output_llh=False):
        """Get the components' responisbilities.

        Parameters
        ----------
        s_stats : numpy.ndarray,
            Sufficient statistics.
        output_llh : boolean
            If True, returns the per component log-likelihood.

        Returns
        -------
        log_norm : numpy.ndarray
            Per-frame log normalization constant.
        resps : numpy.ndarray
            Responsibilities.
        exp_llh : boolean
            If output_llh is True, per component log-likelihood.

        """
        # Expected value of the log-likelihood w.r.t. the posteriors.
        exp_llh = self.comp_params.dot(s_stats.T).T

        # Forward-Backward.
        log_alphas = self.forward(exp_llh)
        log_betas = self.backward(exp_llh)
        log_q_Z = (log_alphas + log_betas).T
        log_norm = logsumexp(log_q_Z, axis=0)
        resps = np.exp((log_q_Z - log_norm))

        return log_norm[-1], resps.T, (exp_llh, log_alphas, log_betas)

    def accumulate_stats(self, s_stats, resps, model_data):
        """Accumulate the sufficient statistics.

        Parameters
        ----------
        s_stats : numpy.ndarray
            Sufficient statistics.
        resps : numpy.ndarray
            Per-frame responsibilities.
        model_data : object
            Model speficic data for the training.

        Returns
        -------
        acc_stats : :class:`EFDStats`
            Accumulated sufficient statistics.

        """
        if self.n_states > 1 :
            acc_stats1 = self.units_stats(*model_data)
        else:
            acc_stats1 = resps.sum(axis=0)
        acc_stats2 = resps.T.dot(s_stats)
        return EFDStats([acc_stats1, acc_stats2])

    def grads_from_acc_stats(self, acc_stats):
        """Compute the gradients from the accumulated statistics.

        Parameters
        ----------
        acc_stats : :class:`EFDStats`
            Accumulated sufficient statistics.

        Returns
        -------
        grads : list
            List of all the gradients.

        """
        grads = []
        grads.append(self.prior.natural_params + acc_stats[0] \
                     - self.posterior.natural_params)

        for idx, stats in enumerate(acc_stats[1]):
            component = self.components[idx]
            grad = component.prior.natural_params + stats - \
                component.posterior.natural_params
            grads.append(grad)

        return grads

    # StdSGAPython interface.
    # ------------------------------------------------------------------

    def gradients(self, data):
        """Compute the natural gradient of the VB lower bound.

        Parameters
        ----------
        data : numpy.ndarray
            (NxD) matrix where N is the number of frames and D is the
            dimension of a single features vector.

        Returns
        -------
        grads : list
            List of gradients.

        """
        # Run the standard VB E-step.
        log_norm, acc_stats = self.vb_e_step(data)

        # Scale the statistics.
        scale = self.n_frames / len(data)
        acc_stats *= scale

        # Estimate the lower bound
        kl_div = self.kl_div_posterior_prior()
        lower_bound = (scale * log_norm.sum() - kl_div) / self.n_frames

        # Compute the (natural) gradients.
        grads = self.grads_from_acc_stats(acc_stats)

        return lower_bound, grads

    def after_grad_update(self):
        """Called after each gradient update."""
        self.posterior.natural_params = self.params[0]
        for idx, component in enumerate(self.components):
            component.posterior.natural_params = self.params[idx + 1]
        self._prepare()

    # Variational Bayes training.
    # ------------------------------------------------------------------

    def vb_e_step(self, data):
        """E-step of the standard Variational Bayes algorithm.

        Parameters
        ----------
        data : numpy.ndarray
            (NxD) matrix where N is the number of frames and D is the
            dimension of a single features vector.

        Returns
        -------
        exp_llh : float
            Expected value of the log-likelihood of the data given the
            model.
        acc_s_stats : numpy.ndarray
            Accumulated sufficient statistics.

        """
        # Sufficient statistics of the data.
        s_stats = self.get_sufficient_stats(data)

        # Compute the per-frame responsibilities.
        log_norm, resps, model_data = self.get_resps(s_stats)

        # Accumulate the statistics.
        return log_norm, self.accumulate_stats(s_stats, resps, model_data)

    def vb_m_step(self, acc_stats):
        """M-step of the standard Variational Bayes algorithm.

        Parameters
        ----------
        acc_stats : :class:`EFDStats`
            Accumulated sufficient statistics.

        """
        self.posterior.natural_params = self.prior.natural_params + \
            acc_stats[0]
        for idx, stats in enumerate(acc_stats[1]):
            self.components[idx].update_posterior(stats)
        self._prepare()

    # ------------------------------------------------------------------
