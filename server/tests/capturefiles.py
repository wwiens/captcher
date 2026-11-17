"""A read-only view of the capture folder, for assertions.

The suites used to run against an in-memory bucket and inspect it directly.
The captures are real files now (conftest gives every test an empty captures
folder under a temporary CAPTCHER_HOME), and this is the same inspection
interface over them: `current().keys()`, `.text(key)`, `.objects`.
"""
from captcher import blobs


class CaptureFiles:
    def keys(self):
        """Every capture key, sorted."""
        return blobs.list_keys("")

    def text(self, key):
        """The file's text, or None when there is no such file."""
        try:
            return blobs.get_text(key)
        except blobs.NotFound:
            return None

    def content_type(self, key):
        return blobs.content_type_for(key)

    @property
    def objects(self):
        """{key: (bytes, content_type)} for every capture file."""
        return {key: (blobs.get(key), blobs.content_type_for(key)) for key in self.keys()}


def current():
    return CaptureFiles()
