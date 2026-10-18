"""Print the yt-dlp extractor that handles a URL, or nothing.

Run as its own process by media.detect() so yt-dlp's extractor list never
stays in the daemon's memory. Only site-specific extractors count: the
generic one claims every URL.
"""

import sys

sys.dont_write_bytecode = True

try:
    from yt_dlp.extractor import gen_extractor_classes
except ImportError:
    sys.exit(0)

url = sys.argv[1] if len(sys.argv) > 1 else ""
for ie in gen_extractor_classes():
    if ie.ie_key() != "Generic" and ie.suitable(url):
        print(ie.ie_key())
        break
