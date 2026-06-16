"""Dummy CRM service (PRD §06 / §10 / Engineering Philosophy §07).

A deliberate abstraction layer, not a compromise: VB_Svc reads customers
through this stable interface, so a bank's production CRM plugs into the same
contract with no rework of the AI pipeline (PRD §12 mitigation).
"""
