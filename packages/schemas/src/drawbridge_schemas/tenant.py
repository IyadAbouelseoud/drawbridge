"""Who the claimant is, as a customs authority needs to see them.

Until now the filing identity arrived in the body of `POST /packaging/build`: the caller
told the packager what to print on a form addressed to CBP or ZATCA. That works for one
tenant whose numbers a developer knows, and it is why week 12 could seed two pilot tenants
and still not file for either of them.

These four identifiers are not metadata. Each one is load-bearing in a different way:

- **EIN** is the claimant of record on a CBP 7551. Wrong, and the refund is claimed by
  somebody else.
- **CR number** identifies the establishment ZATCA holds responsible for the declaration.
- **Broker code** — CBP's three-character filer code — names the licensed filer who
  actually transmits. Drawbridge never files.
- **IBAN** is where money lands. It is the only field here whose error moves cash to a
  stranger, so it is the only one validated by checksum rather than by shape.

Validation is deliberately structural and not authoritative. A mod-97 IBAN check proves
the number was not mistyped; it does not prove the account exists or belongs to the
tenant. A format check on an EIN proves nine digits, not that the IRS issued them. The
distinction is written into the field docs because a caller who reads "validated" as
"verified" will skip the confirmation step that actually matters.
"""

from __future__ import annotations

import re
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drawbridge_schemas.jurisdiction import Jurisdiction

# 9 digits, conventionally printed NN-NNNNNNN. CBP accepts a two-character suffix
# identifying a division of the same taxpayer, which is why this is not just an int.
_EIN = re.compile(r"^\d{2}-?\d{7}(-?[A-Z0-9]{2})?$")

# Saudi commercial registration: 10 digits, historically prefixed by the issuing city's
# code. Stored as digits; the prefix is not separated because a CR is quoted whole.
_CR_NUMBER = re.compile(r"^\d{10}$")

# CBP filer code: three characters, alphanumeric, assigned to a licensed broker or a
# self-filer. Printed on the 7551 and on every ABI transmission.
_BROKER_CODE = re.compile(r"^[A-Z0-9]{3}$")

_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

# Saudi IBANs are exactly 24 characters: SA + 2 check + 2 bank + 18 account. Checked
# separately from the generic pattern so a KSA refund account that is the right shape for
# some other country is still refused.
_SA_IBAN_LENGTH = 24


def _strip(value: str | None) -> str | None:
    """Normalise a printed identifier to its canonical form.

    Humans transcribe these from letterhead with spaces in them. Whitespace inside an
    IBAN is how banks print it, not part of the number.
    """
    if value is None:
        return None
    collapsed = "".join(value.split()).upper()
    return collapsed or None


def iban_checksum_ok(iban: str) -> bool:
    """ISO 13616 mod-97. True when the check digits are self-consistent.

    Moving the first four characters to the end and mapping letters to numbers must leave
    a number congruent to 1 mod 97. This catches a transposed pair of digits, which is the
    error a human actually makes and the one that silently sends a refund elsewhere.
    """
    rotated = iban[4:] + iban[:4]
    digits = "".join(str(ord(char) - 55) if char.isalpha() else char for char in rotated)
    if not digits.isdigit():
        return False
    return int(digits) % 97 == 1


class TenantProfile(BaseModel):
    """The filing identity of one tenant, in both jurisdictions it may file in.

    Every field is optional at this layer and required at the point of filing. A tenant
    that files only in the US has no CR number and should not be forced to invent one, but
    a US packet built without an EIN is not a weak packet — it is unfileable, and
    `require_for` is what says so at the moment it matters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    legal_name: Annotated[str, Field(min_length=1, max_length=255)]
    """Name as registered, not a trading name. It is what appears on the form."""

    # ------------------------------------------------------------------ US
    ein: Annotated[str | None, Field(default=None, max_length=16)] = None
    """IRS employer identification number. Claimant of record on a CBP 7551."""

    broker_code: Annotated[str | None, Field(default=None, max_length=3)] = None
    """CBP filer code of the licensed broker who transmits. Drawbridge never files."""

    # ------------------------------------------------------------------ KSA
    cr_number: Annotated[str | None, Field(default=None, max_length=10)] = None
    """Saudi commercial registration number of the establishment."""

    vat_number: Annotated[str | None, Field(default=None, max_length=15)] = None
    """ZATCA VAT registration, 15 digits. Carried because a refund settles against the
    same taxpayer record and a mismatch between the two stalls the file."""

    iban: Annotated[str | None, Field(default=None, max_length=34)] = None
    """Refund destination. ZATCA settles an approved claim to a registered account."""

    # ------------------------------------------------------------------ address
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    postal_code: str = ""
    country: Annotated[str, Field(default="", max_length=2)] = ""
    """ISO 3166-1 alpha-2. Empty until set; never defaulted to US."""

    contact_email: str = ""
    contact_phone: str = ""

    @field_validator("ein", "broker_code", "cr_number", "vat_number", "iban", mode="before")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        return _strip(value) if isinstance(value, str) else value

    @field_validator("ein")
    @classmethod
    def _check_ein(cls, value: str | None) -> str | None:
        """Accept the printed form, store the canonical one.

        People transcribe an EIN as `95-4417293` because that is how it appears on an IRS
        notice. Stored with the hyphen, it is a different string from the same number
        entered without one, and two spellings of one identifier is how a tenant acquires
        two identities. The hyphen is presentation and `ein_display` puts it back.
        """
        if value is None:
            return None
        if not _EIN.match(value):
            msg = "EIN must be 9 digits, optionally with a two-character CBP suffix"
            raise ValueError(msg)
        return value.replace("-", "")

    @field_validator("broker_code")
    @classmethod
    def _check_broker(cls, value: str | None) -> str | None:
        if value is not None and not _BROKER_CODE.match(value):
            msg = "broker code is CBP's three-character filer code"
            raise ValueError(msg)
        return value

    @field_validator("cr_number")
    @classmethod
    def _check_cr(cls, value: str | None) -> str | None:
        if value is not None and not _CR_NUMBER.match(value):
            msg = "Saudi commercial registration number is 10 digits"
            raise ValueError(msg)
        return value

    @field_validator("vat_number")
    @classmethod
    def _check_vat(cls, value: str | None) -> str | None:
        if value is not None and not (len(value) == 15 and value.isdigit()):
            msg = "ZATCA VAT registration number is 15 digits"
            raise ValueError(msg)
        return value

    @field_validator("iban")
    @classmethod
    def _check_iban(cls, value: str | None) -> str | None:
        """Shape, then checksum. Both, because either alone lets a real error through."""
        if value is None:
            return None
        if not _IBAN.match(value):
            msg = "IBAN must be two letters, two check digits, then 11-30 alphanumerics"
            raise ValueError(msg)
        if not iban_checksum_ok(value):
            msg = "IBAN check digits do not validate (ISO 13616 mod-97)"
            raise ValueError(msg)
        if value.startswith("SA") and len(value) != _SA_IBAN_LENGTH:
            msg = f"a Saudi IBAN is {_SA_IBAN_LENGTH} characters"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _country_is_upper(self) -> Self:
        if self.country and self.country != self.country.upper():
            msg = "country must be an uppercase ISO 3166-1 alpha-2 code"
            raise ValueError(msg)
        return self

    def missing_for(self, jurisdiction: Jurisdiction) -> tuple[str, ...]:
        """Fields this profile lacks before it can address a packet in that jurisdiction.

        The address is common to both and is included: a 7551 and a ZATCA refund request
        both print a claimant address, and a packet that omits it is returned.

        `broker_code` is **not** required. A self-filer has none, and demanding one would
        force a placeholder into a field CBP reads as naming a licensed broker.
        """
        common = {
            "legal_name": self.legal_name,
            "address_line1": self.address_line1,
            "city": self.city,
            "country": self.country,
        }
        specific: dict[str, str | None] = (
            {"ein": self.ein}
            if jurisdiction is Jurisdiction.US
            else {"cr_number": self.cr_number, "iban": self.iban}
        )
        return tuple(name for name, value in {**common, **specific}.items() if not value)

    def require_for(self, jurisdiction: Jurisdiction) -> None:
        """Raise unless this profile can address a packet in that jurisdiction.

        Called at packet build, not at profile save. A tenant is onboarded before its
        paperwork is complete, and refusing to store a partial profile would mean the
        missing field lives in somebody's inbox instead of in the database.
        """
        missing = self.missing_for(jurisdiction)
        if missing:
            msg = (
                f"tenant profile cannot address a {jurisdiction.value.upper()} packet: "
                f"missing {', '.join(missing)}"
            )
            raise ValueError(msg)

    @property
    def ein_display(self) -> str:
        """`95-4417293` — the form a human checks against an IRS notice.

        Stored without the hyphen; printed with it, because a filer proofreading a 7551 is
        comparing against a document that has one.
        """
        if not self.ein:
            return ""
        return f"{self.ein[:2]}-{self.ein[2:9]}{self.ein[9:]}"

    @property
    def identifier_for_us(self) -> str:
        """What prints in the 7551 claimant identifier box."""
        return self.ein_display

    @property
    def identifier_for_ksa(self) -> str:
        """What identifies the establishment to ZATCA."""
        return self.cr_number or ""

    def identifier_for(self, jurisdiction: Jurisdiction) -> str:
        return (
            self.identifier_for_us if jurisdiction is Jurisdiction.US else self.identifier_for_ksa
        )
