"""Bounded multiprocessing packaging check. Never loads a printer or a credential."""
from __future__ import annotations

import multiprocessing


def _probe(connection):
    from . import __version__
    try:
        connection.send({'version': __version__, 'physicalPrinting': False})
    finally:
        connection.close()


def check_spawn(timeout=10):
    from . import __version__
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_probe, args=(child,))
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise RuntimeError('Packaged subprocess did not respond')
        result = parent.recv()
        process.join(timeout=2)
        if process.exitcode != 0 or result != {'version': __version__, 'physicalPrinting': False}:
            raise RuntimeError('Packaged subprocess failed')
        return True
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        process.close()
