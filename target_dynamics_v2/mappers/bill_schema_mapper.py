import singer

from target_dynamics_v2.mappers.attachment_schema_mapper import AttachmentSchemaMapper
from target_dynamics_v2.mappers.base_mappers import BaseMapper
from target_dynamics_v2.mappers.bill_comment_schema_mapper import BillCommentSchemaMapper
from target_dynamics_v2.mappers.bill_line_item_schema_mapper import BillLineItemSchemaMapper
from target_dynamics_v2.mappers.bill_expense_item_schema_mapper import BillExpenseItemSchemaMapper
from target_dynamics_v2.utils import RecordNotFound

LOGGER = singer.get_logger("target-dynamics-v2")


class BillSchemaMapper(BaseMapper):
    name = "Bills"
    existing_record_pk_mappings = [
        {"record_field": "id", "dynamics_field": "id", "required_if_present": True},
        {"record_field": "transactionNumber", "dynamics_field": "number", "required_if_present": False}
    ]

    field_mappings = {
        "billNumber": "vendorInvoiceNumber",
        "dueDate": "dueDate",
        "issueDate": "invoiceDate",
        "postingDate": "postingDate",
    }

    def to_dynamics(self) -> dict:
        self._validate_company()

        payload = {
            **self._map_internal_id(),
            **self._map_vendor(required=True),
            **self._map_currency(),
            **self._map_dimension_set_lines()
        }

        self._map_fields(payload)

        # whtTaxCode only exists on the custom Precoro API entity, never on the standard one
        wht_tax_code = self.record.get("whtTaxCode")
        if wht_tax_code is not None and self.sink.dynamics_client.custom_api_entity_has_field("purchaseInvoice", "whtTaxCode"):
            payload["whtTaxCode"] = wht_tax_code

        self._map_bill_line_items(payload)
        self._map_attachments(payload)

        return {"payload": payload, "company_id": self.company["id"]}

    def _map_bill_line_items(self, payload):
        mapped_line_items = []
        existing_lines = self.existing_record.get("purchaseInvoiceLines", []) if self.existing_record else []

        line_items = self.record.get("lineItems", [])
        missing_items = []
        for line_item in line_items:
            line_item["subsidiaryId"] = self.company["id"]
            try:
                line_payload = BillLineItemSchemaMapper(line_item, self.sink, self.reference_data, existing_lines).to_netsuite()
            except RecordNotFound as e:
                missing_items.append(str(e))
                continue
            mapped_line_items.append(line_payload)

        if missing_items:
            LOGGER.error(
                "Bill not exported to Business Central: the following items were not found:\n"
                + "\n".join(f"- {item}" for item in missing_items)
                + "\n\nPlease make sure these items exist in Business Central and their name and code match exactly."
            )
            # Abort this Bill entirely rather than sending it to BC with missing/empty
            # lines - BC accepts an incomplete purchaseInvoiceLine and fails later with an
            # unrelated, confusing error (e.g. Application_StringExceededLength).
            raise RecordNotFound(f"{len(missing_items)} item(s) not found in Business Central: {', '.join(missing_items)}")

        expense_items = self.record.get("expenses", [])
        for expense_item in expense_items:
            expense_item["subsidiaryId"] = self.company["id"]
            expense_line_payload = BillExpenseItemSchemaMapper(expense_item, self.sink, self.reference_data, existing_lines).to_netsuite()
            mapped_line_items.append(expense_line_payload)

        comments = self.record.get("comments", [])
        for comment in comments:
            comment_payload = BillCommentSchemaMapper(comment, self.sink, self.reference_data, existing_lines).to_netsuite()
            mapped_line_items.append(comment_payload)

        if mapped_line_items:
            payload["purchaseInvoiceLines"] = mapped_line_items

    def _map_attachments(self, payload):
        attachments = self.record.get("attachments", [])

        
        mapped_attachments = []
        for attachment in attachments:
            attachment_payload = AttachmentSchemaMapper({
                "fileName": attachment,
                "parentId": payload.get("id"),
                "parentType": "Purchase Invoice",
                "subsidiaryId": self.company["id"]
            }, self.sink, self.reference_data).to_dynamics()
            mapped_attachments.append(attachment_payload)

        if mapped_attachments:
            payload["attachments"] = mapped_attachments
