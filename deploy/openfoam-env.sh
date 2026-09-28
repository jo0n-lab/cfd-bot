# Sourced by the queue launcher. systemd does not read the user's .zshrc.
# SYSTEMOPENMPI needs mpicc on PATH before OpenFOAM discovers its MPI libraries.
source /usr/share/Modules/init/bash || return $?
module load mpi/openmpi-x86_64 || return $?
source /opt/OpenFOAM-dev/etc/bashrc || return $?
