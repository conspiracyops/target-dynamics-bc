from typing import List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.mappers.item_schema_mapper import ItemSchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkBatchUpsert

class ItemSink(DynamicsBaseBatchSinkBatchUpsert):
    name = "Items"
    record_type = "Items"

    def preprocess_batch(self, records: List[dict]):
        # fetch reference data related to existing items
        filter_mappings = [
            {"field_from": "id", "field_to": "id", "should_quote": False},
            {"field_from": "number", "field_to": "number", "should_quote": True},
            {"field_from": "displayName", "field_to": "displayName", "should_quote": True}
        ]

        existing_company_items = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            filter_mappings
        )

        self.reference_data = {**self._target.reference_data, self.name: existing_company_items}

    def process_batch_record(self, record: dict) -> dict:
        # perform the mapping
        mapped_record = ItemSchemaMapper(record, self, self.reference_data)
        payload = mapped_record.to_dynamics()

        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, mapped_record.company["id"], payload.get("id"))

        records = [{"payload": payload, "request_params": request_params }]

        return {"records": records}
