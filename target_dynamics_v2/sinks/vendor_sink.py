from typing import List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.mappers.vendor_schema_mapper import VendorSchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkBatchUpsert

class VendorSink(DynamicsBaseBatchSinkBatchUpsert):
    name = "Vendors"
    record_type = "Vendors"

    def _get_output_currency(self, record: dict, payload: dict, company: dict):
        if currency_id := payload.get("currencyId"):
            found_currency = next(
                (currency for currency in company.get("currencies", []) if currency.get("id") == currency_id),
                None
            )
            if found_currency:
                return found_currency.get("code") or found_currency.get("displayName") or found_currency.get("id")

        if currency_code := payload.get("currencyCode"):
            return currency_code

        return record.get("currency") or record.get("currencyName") or record.get("currencyId")

    def preprocess_batch(self, records: List[dict]):
        # fetch reference data related to existing vendors
        filter_mappings = [
            {"field_from": "id", "field_to": "id", "should_quote": False},
            {"field_from": "vendorNumber", "field_to": "number", "should_quote": True}
        ]

        existing_company_vendors = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            filter_mappings,
            expand="defaultDimensions"
        )

        self.reference_data = {**self._target.reference_data, self.name: existing_company_vendors}

    def process_batch_record(self, record: dict) -> dict:
        # perform the mapping
        mapped_record = VendorSchemaMapper(record, self, self.reference_data)
        payload = mapped_record.to_dynamics()

        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, mapped_record.company["id"], payload.get("id"))

        default_dimensions_requests = []
        if mapped_record.existing_record and payload.get("defaultDimensions"):
            default_dimensions = payload.pop("defaultDimensions")
            default_dimensions_requests = DynamicsClient.create_default_dimensions_requests(
                self.record_type,
                mapped_record.company["id"],
                payload["id"],
                default_dimensions
            )

        records = [{"payload": payload, "request_params": request_params }]
        records += default_dimensions_requests

        return {
            "records": records,
            "state_fields": {
                "name": record.get("vendorName"),
                "mapField": self._get_output_currency(record, payload, mapped_record.company),
                "companyId": mapped_record.company["id"],
            },
        }
