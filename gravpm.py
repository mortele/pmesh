from gaepsi2.cosmology import WMAP7 as cosmology
from bigfile import BigFile 
from mpi4py import MPI
from pypm.particlemesh import ParticleMesh
import numpy
from pypm.transfer import TransferFunction

if __name__ == '__main__':

#    from matplotlib import pyplot

    # this will set the units to
    #
    # time: 980 Myear/h
    # distance: 1 Kpc/h
    # speed: 100 km/s
    # mass: 1e10 Msun /h

    DH = 3e5 / 100.
    G = 43007.1
    H0 = 0.1
    Nmesh = 64
    file = BigFile('debug-32/IC')
    header = file.open('header')
    BoxSize = header.attrs['BoxSize'][0]
    a0 = header.attrs['Time'][0]

    Ntot = file.open('1/ID').size
    myslice = slice(
            MPI.COMM_WORLD.rank * Ntot // MPI.COMM_WORLD.size,
            (MPI.COMM_WORLD.rank + 1) * Ntot // MPI.COMM_WORLD.size,
            )
    P = lambda : None
    P.Mass = header.attrs['MassTable'][1]
    P.Pos = file.open('1/Position')[myslice] * (1.0 * Nmesh / BoxSize )
    P.Vel = file.open('1/Velocity')[myslice] * a0 ** 1.5
    P.ID = file.open('1/ID')[myslice] 
    
    NumPart = len(P.Pos)
    print Nmesh, BoxSize, P.Mass
    #NumPart = Nmesh ** 3 // 8 / MPI.COMM_WORLD.size


    pm = ParticleMesh(Nmesh, verbose=False)
    #pos = numpy.random.random(size=(NumPart, 3)) * Nmesh
    #pos = numpy.array(
    #    numpy.indices((Nmesh, Nmesh, Nmesh)), 
    #    dtype='f4').reshape(3, -1).T.copy()
    #MPI.COMM_WORLD.rank * NumPart + numpy.arange(len(pos), dtype='i4')
    #vel = numpy.zeros_like(pos)

    P.Accel = numpy.empty_like(P.Pos)

    def canonical_factors(loga0, loga1, loga2):
        """returns canonical factors for
                kickA, drift, kickB
        """
        N = 129
        g1 = numpy.linspace(loga0, loga1, N, endpoint=True)
        g2 = numpy.linspace(loga1, loga2, N, endpoint=True)
        a1 = numpy.exp(g1)
        a2 = numpy.exp(g2)
        E1 = cosmology.Ea(a1) * H0
        E2 = cosmology.Ea(a2) * H0
        return (
                numpy.trapz(1 / ( a1 * E1), g1),
                numpy.trapz(1 / ( a1 * a1 * E1), g1)
              + numpy.trapz(1 / ( a2 * a2 * E2), g2),
                numpy.trapz(1 / ( a2 * E2), g2),
                )

    dloga = 0.2
    std = None

    loga0 = numpy.log(a0)
    loga = loga0
    vel2 = None
    accel2 = None
    icps = None
    while True:
        # lets get the correct mass distribution with particles on the edge mirrored
        layout = pm.decompose(P.Pos)
        tpos = layout.exchange(P.Pos)
        pm.r2c(tpos, P.Mass)

        # ok. get the smoothed density
        density = pm.c2r(
            tpos, 
            TransferFunction.Constant(BoxSize ** -3),
            TransferFunction.Trilinear,
            TransferFunction.Gaussian(1.25 * 2.0 ** 0.5), 
            TransferFunction.Trilinear,
            )

        wout = numpy.empty(64)
        psout = numpy.empty(64)
        pm.c2r(
            tpos, 
            TransferFunction.Constant(BoxSize ** -3),
            TransferFunction.NormalizeDC,
            TransferFunction.RemoveDC,
            TransferFunction.Trilinear,
    #        TransferFunction.Gaussian(1.25 * 2.0 ** 0.5), 
            # move to Mpc/h units
            TransferFunction.Constant((BoxSize / 1000. / Nmesh) ** 3),
            TransferFunction.PowerSpectrum(wout, psout)
            )

        wout /= (BoxSize / 1000. / Nmesh)
        if icps is None:
            icps = psout.copy()

        if MPI.COMM_WORLD.rank == 0:
            print 'k', wout
            print 'Pk', psout
            print 'power spectrum / IC', psout / icps, \
                (numpy.exp(loga) / numpy.exp(loga0)) ** 2
            #pyplot.plot(wout, psout)
            #pyplot.xscale('log')
            #pyplot.yscale('log')
            #pyplot.draw()
            #pyplot.show()

        density = layout.gather(density, mode='sum')
        Ntot = MPI.COMM_WORLD.allreduce(len(density), MPI.SUM)
        mean = MPI.COMM_WORLD.allreduce(
                numpy.einsum('i->', density, dtype='f8'), MPI.SUM) / Ntot
        std = (MPI.COMM_WORLD.allreduce(numpy.einsum('i,i->', density, density, dtype='f8'), MPI.SUM) /
                Ntot - mean **2)

        dt_kickA, dt_drift, dt_kickB = canonical_factors(
                loga, loga + 0.5 * dloga, loga + dloga)


        for d in range(3):
            tmp = pm.c2r(
                tpos, 
                # to rho_k in comoving units
#                TransferFunction.Inspect('PRE', (0, 0, 1)),
                TransferFunction.Constant(BoxSize ** -3),
                TransferFunction.RemoveDC,
                TransferFunction.Trilinear,
                TransferFunction.Gaussian(1.25 * 2.0 ** 0.5), 
                TransferFunction.Poisson, 
                TransferFunction.Constant(4 * numpy.pi * G),
                TransferFunction.Constant(Nmesh ** -2 * BoxSize ** 2),
                TransferFunction.Trilinear,
#                TransferFunction.Inspect('POT', (0, 0, 1)),
                TransferFunction.SuperLanzcos(d), 
                TransferFunction.Constant(- Nmesh ** 1 * BoxSize ** -1),
#                TransferFunction.Inspect('ACC', (0, 0, 1))
                )
            tmp = layout.gather(tmp, mode='sum')
            # now lets flip the sign of gravity and build a glass
            #tmp *= -1
            P.Accel[:, d] = tmp

        vel2 = MPI.COMM_WORLD.allreduce(numpy.einsum('ij,ij->', P.Vel, P.Vel,
            dtype='f8'), MPI.SUM) ** 0.5
        accel2 = MPI.COMM_WORLD.allreduce(numpy.einsum('ij,ij->', P.Accel, P.Accel,
            dtype='f8'), MPI.SUM) ** 0.5

        if MPI.COMM_WORLD.rank == 0:
            print 'step', \
            'a',  numpy.exp(loga), \
            'mean density', mean, 'std', std, \
            'Ntot', Ntot, 'vel std', vel2, 'accel std', accel2, \
            'dt', dt_kickA, dt_drift, dt_kickB
            print P.Pos[0] / Nmesh * BoxSize, P.Vel[0], P.Accel[0], P.ID[0]

        if loga >= 0.0: break
        
        P.Vel += P.Accel * dt_kickA

        P.Pos += P.Vel * dt_drift * (1. / BoxSize * Nmesh)

        P.Pos %= Nmesh

        P.Vel += P.Accel * dt_kickB

        loga += dloga
