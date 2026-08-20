from target_dynamics_v2.mappers.base_mappers import BaseMapper
from target_dynamics_v2.utils import InvalidFieldValue

class JournalEntryLineSchemaMapper(BaseMapper):
    name = "JournalEntryLine"

    field_mappings = {
        "transactionDate": "postingDate",
        "documentNumber": "documentNumber",
        "description": "description"
    }

    def to_dynamics(self) -> dict:
        payload = {
            **self._map_account(required=True),
            **self._map_amount(),
            **self._map_dimension_set_lines()
        }

        self._map_fields(payload)

        return payload
    
    def _map_amount(self):
        entry_type = self.record.get("entryType")
        if entry_type not in ["Credit", "Debit"]:
            raise InvalidFieldValue(f"'{entry_type}' is an invalid field value for 'entryType'. It should one of 'Credit' or 'Debit'")

        if entry_type == "Credit":
            amount = abs(self.record.get("creditAmount")) * -1.0
        else:
            amount = abs(self.record.get("debitAmount"))

        return {"amount": amount}
