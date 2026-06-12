from target_dynamics_v2.mappers.base_mappers import BaseMapper

class BillLineItemSchemaMapper(BaseMapper):
    name = "BillLines"
    field_mappings = {
        "externalId": "sequence",
        "description": "description",
        "taxCode": "taxCode",
        "discount": "discountAmount",
        "quantity": "quantity",
        "unitPrice": "unitCost",
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
            **self._map_item(),
            **self._map_location(),
            **self._map_dimension_set_lines(),
            "lineType": "Item"
        }

        self._map_fields(payload)
        self._map_purchase_order_number(payload, "purchaseInvoiceLine")

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

        record_item = self._map_item()
        if (record_item_id := record_item.get("itemId")) and (record_description := self.record.get("description")):
            found_record = next(
                (line for line in self.existing_lines
                if line["description"] == record_description and line["itemId"] == record_item_id),
                None
            )

        if found_record:
            return found_record
 
    def _map_item(self):
        found_item = None

        items_reference_data = self.reference_data.get("Items", {}).get(self.company["id"], [])

        if item_id := self.record.get("itemId"):
            found_item = next(
                (item for item in items_reference_data
                if item["id"] == item_id),
                None
            )

        if (item_external_id := self.record.get("itemExternalId")) and not found_item:
            found_item = next(
                (item for item in items_reference_data
                if item["number"] == item_external_id),
                None
            )

        if (item_name := self.record.get("itemExternalName")) and not found_item:
            found_item = next(
                (item for item in items_reference_data
                if item["displayName"] == item_name),
                None
            )
        
        item_info = {}

        if found_item:
            item_info = {"itemId": found_item["id"]}

        return item_info
