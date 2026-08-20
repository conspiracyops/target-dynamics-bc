import hashlib
from target_dynamics_v2.mappers.base_mappers import BaseMapper
from target_dynamics_v2.mappers.journal_entry_line_schema_mapper import JournalEntryLineSchemaMapper
from target_dynamics_v2.utils import InvalidInputError, MissingField, RecordNotFound

class JournalEntrySchemaMapper(BaseMapper):
    name = "Journals"
    existing_record_pk_mappings = [
        {"record_field": "externalId", "dynamics_field": "displayName", "required_if_present": False}
    ]

    field_mappings = {
        "externalId": "displayName"
    }

    def to_dynamics(self) -> dict:
        self._validate_company()
        self._validate_external_id()
        self._validate_transaction_date()

        payload = {
            **self._map_internal_id(),
            **self._map_journal_entry_lines()
        }

        self._map_fields(payload)

        
        payload["code"] = hashlib.sha256(payload["displayName"].encode()).hexdigest()[:10]

        is_draft = self.record.get("state") == "draft"

        return {"payload": payload, "is_draft": is_draft, "company_id": self.company["id"]}

    def _validate_external_id(self):
        external_id = self.record.get("externalId")
        if not external_id:
            raise MissingField(f"The required field 'externalId' was not provided")

        if len(external_id) > 20:
            raise InvalidInputError(f"The length of externalId={external_id} should be less or equal to 20.")

    def _validate_transaction_date(self):
        if not self.record.get("transactionDate"):
            raise MissingField(f"The required field 'transactionDate' was not provided")

    def _map_journal_entry_lines(self):
        lines = []
        transaction_date = self.record.get("transactionDate")

        if not self.record.get("lineItems", []):
            raise MissingField(f"The required field 'lineItems' was not provided")

        lines_amount_sum = 0
        for item in self.record.get("lineItems", []):
            item.update(
                {
                    "subsidiaryId": self.record.get("subsidiaryId"),
                    "subsidiaryName": self.record.get("subsidiaryName"),
                    "transactionDate": transaction_date,
                    "documentNumber": self.record.get("externalId")
                }
            )

            mapped_line_item = JournalEntryLineSchemaMapper(item, self.sink, self.reference_data).to_dynamics()
            lines_amount_sum += mapped_line_item["amount"]
            lines.append(mapped_line_item)

        if lines_amount_sum != 0:
            raise InvalidInputError(f"The Journal is out of balance by {lines_amount_sum}. Please check that the Amount is correct for each line")

        return {"journalLines": lines}
