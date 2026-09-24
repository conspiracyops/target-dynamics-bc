from target_dynamics_v2.mappers.base_mappers import BaseMapper

class ItemSchemaMapper(BaseMapper):
    name = "Items"
    existing_record_pk_mappings = [
        {"record_field": "id", "dynamics_field": "id", "required_if_present": True},
        {"record_field": "displayName", "dynamics_field": "displayName", "required_if_present": False},
        {"record_field": "number", "dynamics_field": "number", "required_if_present": False}
    ]
    
    field_mappings = {
        "displayName": "displayName",
        "unitPrice": "unitPrice",
        "number": "number"
    }

    def to_dynamics(self) -> dict:
        self._validate_company()

        payload = {
            **self._map_internal_id(),
        }

        self._map_fields(payload)

        if not self.existing_record:
            # BC blocks Type changes once an item has ledger/purchase/planning
            # activity, so only send it on creation
            if (item_type := self.record.get("type")) is not None:
                payload["type"] = item_type

            self._map_creation_defaults(payload)

        return payload

    def _map_creation_defaults(self, payload):
        """Apply per-company defaults for fields BC requires on creation."""
        item_defaults = self.sink._target.item_defaults.get(self.company["id"], {})

        if gen_prod_posting_group := item_defaults.get("genProdPostingGroupCode"):
            payload["generalProductPostingGroupCode"] = gen_prod_posting_group

        if base_unit_of_measure := item_defaults.get("baseUnitOfMeasureCode"):
            payload["baseUnitOfMeasureCode"] = base_unit_of_measure
