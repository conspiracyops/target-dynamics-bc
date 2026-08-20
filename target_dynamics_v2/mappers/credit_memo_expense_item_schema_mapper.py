from target_dynamics_v2.mappers.base_mappers import BaseMapper

class CreditMemoExpenseItemSchemaMapper(BaseMapper):
    name = "CreditMemoLines"
    field_mappings = {
        "externalId": "sequence",
        "description": "description",
        "taxCode": "taxCode",
        "discount": "discountAmount",
        "amount": "unitCost",
        "irpfTaxPercent": "irpfTaxPercent",
        "irpfTaxAmount": "irpfTaxAmount",
        "quantity": "quantity",
        "vendorContractNumber": "vendorContractNumber",
    }

    def __init__(
            self,
            record,
            sink,
            reference_data,
            existing_lines
    ) -> None:
        self.existing_lines = existing_lines
        super().__init__(record, sink, reference_data)

    def to_netsuite(self) -> dict:
        payload = {
            **self._map_internal_id(),
            **self._map_account(required=True),
            **self._map_location(),
            **self._map_dimension_set_lines(),
            "lineType": "Account",
        }

        self._map_fields(payload)
        self._map_purchase_order_number(payload, "purchaseCreditMemoLine")

        return payload

    def _find_existing_record(self, reference_list):
        """Finds an existing record in the reference data by matching internal.
        """
        if self.company is None:
            return None

        found_record = None

        if record_external_id := self.record.get("externalId"):
            found_record = next(
                (line for line in self.existing_lines
                if line["sequence"] == record_external_id),
                None
            )

        record_account = self._map_account()
        if (record_account_id := record_account.get("accountId")) and (record_description := self.record.get("description")):
            found_record = next(
                (line for line in self.existing_lines
                if line["description"] == record_description and line["accountId"] == record_account_id),
                None
            )

        if found_record:
            return found_record
