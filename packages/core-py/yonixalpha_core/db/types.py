"""Column types that keep credentials out of stored records (2026-10-15).

ScrubbedJSONB / ScrubbedString are JSONB / VARCHAR in the database (no
migration: the column type does not change) that run every value through
redact.scrub_credentials when it is written. This works for ORM objects and
for Core insert()/update() statements alike, so an exception message that
embeds an RPC URL with its key cannot reach a decision reason, a system
event or an audit entry. URLs that cannot carry a credential (token
metadata, IPFS, explorers) are stored unchanged; rows written before this
change are not rewritten.
"""

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from yonixalpha_core.redact import scrub_credentials


class ScrubbedJSONB(TypeDecorator):
    impl = JSONB
    cache_ok = True

    def coerce_compared_value(self, op, value):
        # Comparisons keep JSONB's own typing: the key in `col ? 'key'`
        # (has_key) is TEXT, not this type, so the SQL stays `jsonb ? text`.
        return self.impl.coerce_compared_value(op, value)

    def process_bind_param(self, value, dialect):
        return scrub_credentials(value) if value is not None else value


class ScrubbedString(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return scrub_credentials(value) if isinstance(value, str) else value
