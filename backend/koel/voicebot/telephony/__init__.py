"""PSTN telephony (1.1 Twilio, 1.4 Exotel).

Carrier-agnostic building blocks for bridging a phone call into the existing
orchestrator: a µ-law codec (codec.py), the carrier contract (carrier.py), and
the Twilio Media Streams protocol + webhook signature verification (twilio.py).
The orchestrator is already transport-agnostic (it only needs a ``send``
callback), so the full bridge wires these to it; the pieces here are the
verifiable, carrier-neutral substrate and are dormant until a carrier is
configured.
"""
