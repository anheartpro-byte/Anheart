"""Session records, schema 2.

``schema``, ``rows``, ``codec``, ``ecg``, ``writer`` and ``reader`` are the
format, shared by the console and the simulation. ``journal`` is the bounded
queue and the one thread that writes on the console, so that no write ever
happens in the control loop; ``session`` is the console's adapter, which turns
what its loop sees into the format.
"""
