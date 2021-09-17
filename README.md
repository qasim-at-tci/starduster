# Starduster
Starduster provides a deep learning framework to emulate dust radiative transfer simulations, which significantly accelerates the computation of dust attenuation and emission. Starduster contains two specific generative models, which should be trained by a set of characteristic outputs of a radiative transfer simulation. The obtained neural networks can produce realistic galaxy spectral energy distributions that satisfy the energy balance condition of dust attenuation and emission. Applications of Starduster include SED-fitting and SED-modelling from semi-analytic models. The code is written in PyTorch. Accordingly, users can take advantage of GPU parallelisation and automatic differentiation implemented by PyTorch throughout the applications.