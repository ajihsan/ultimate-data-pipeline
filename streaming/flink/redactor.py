import hashlib
import re


class PIIRedactor:
    """Mask and hash potentially sensitive textual fields."""

    MASK_PLACEHOLDER = "[REDACTED]"

    def __init__(self, salt: str = "default_salt"):
        """Initialize redactor with hashing salt.

        Args:
            salt: Salt value used for deterministic hashing.
        """
        self.salt = salt.encode()
        self._detector_patterns = {
            "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
            "phone": re.compile(
                r"(?:\+?1[-.\s]?)?(?:\(?[0-9]{3}\)?[-.\s]?)?[0-9]{3}[-.\s]?[0-9]{4}"
            ),
            "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
            "ssn": re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),
            "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        }

    def hash_value(self, value: str) -> str:
        """Return deterministic hash for input text.

        Args:
            value: Raw value to hash.

        Returns:
            str: Short hexadecimal hash.
        """
        if not value:
            return ""
        return hashlib.sha256(self.salt + value.encode()).hexdigest()[:16]

    def mask(self, text: str, pii_type: str) -> str:
        """Mask detected PII type in text.

        Args:
            text: Raw text payload.
            pii_type: Detector pattern key.

        Returns:
            str: Text with masked matches.
        """
        pattern = self._detector_patterns.get(pii_type)
        if not pattern or not text:
            return text
        return pattern.sub(self.MASK_PLACEHOLDER, text)

    def mask_all(self, text: str) -> str:
        """Mask all supported PII patterns in text.

        Args:
            text: Raw text payload.

        Returns:
            str: Masked text.
        """
        if not text:
            return ""
        result = text
        for pii_type in self._detector_patterns:
            result = self.mask(result, pii_type)
        return result

    def redact_text(self, text: str | None) -> str:
        """Redact nullable text field.

        Args:
            text: Input text or None.

        Returns:
            str: Redacted text.
        """
        if not text:
            return ""
        return self.mask_all(text)

    def hash_username(self, username: str) -> str:
        """Hash username for anonymized storage.

        Args:
            username: Raw username.

        Returns:
            str: Deterministic user hash.
        """
        return self.hash_value(username)
