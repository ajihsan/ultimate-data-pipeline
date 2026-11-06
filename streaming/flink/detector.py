import re
from typing import Optional


class PIIDetector:
    """PII detector based on regex patterns."""

    EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    PHONE_PATTERN = re.compile(r"(?:\+?1[-.\s]?)?(?:\(?[0-9]{3}\)?[-.\s]?)?[0-9]{3}[-.\s]?[0-9]{4}")
    CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
    SSN_PATTERN = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")
    IP_ADDRESS_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    PATTERNS = {
        "email": EMAIL_PATTERN,
        "phone": PHONE_PATTERN,
        "credit_card": CREDIT_CARD_PATTERN,
        "ssn": SSN_PATTERN,
        "ip_address": IP_ADDRESS_PATTERN,
    }

    def detect(self, text: str) -> tuple[bool, list[str]]:
        """Detect PII types in a text payload.

        Args:
            text: Input text.

        Returns:
            Tuple[bool, List[str]]: Whether any PII was found and detected types.
        """
        if not text:
            return False, []

        detected_types = []
        for pii_type, pattern in self.PATTERNS.items():
            if pattern.search(text):
                detected_types.append(pii_type)

        return len(detected_types) > 0, detected_types

    def detect_all(self, fields: dict[str, Optional[str]]) -> tuple[bool, list[str]]:
        """Detect PII over multiple text fields.

        Args:
            fields: Mapping of field name to text value.

        Returns:
            Tuple[bool, List[str]]: Whether any PII was found and deduplicated types.
        """
        all_detected = []
        for field_name, value in fields.items():
            if value:
                _, detected = self.detect(value)
                all_detected.extend(detected)

        return len(all_detected) > 0, list(set(all_detected))
