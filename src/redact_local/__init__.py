"""redact-local: de-identify matter documents on this machine before an AI reads them.

Nothing heavy is imported here. Modules that pull in Presidio, spaCy, PyMuPDF or
RapidOCR install the network guard first (see netguard.py).
"""

__version__ = "0.1.0"
