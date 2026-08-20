from target_dynamics_v2.mappers.attachment_schema_mapper import AttachmentSchemaMapper
from target_dynamics_v2.mappers.base_mappers import BaseMapper
from target_dynamics_v2.mappers.bill_comment_schema_mapper import BillCommentSchemaMapper
from target_dynamics_v2.mappers.credit_memo_line_item_schema_mapper import CreditMemoLineItemSchemaMapper
from target_dynamics_v2.mappers.credit_memo_expense_item_schema_mapper import CreditMemoExpenseItemSchemaMapper


class CreditMemoSchemaMapper(BaseMapper):
    name = "CreditMemos"
    existing_record_pk_mappings = [
        {"record_field": "id", "dynamics_field": "id", "required_if_present": True},
        {"record_field": "transactionNumber", "dynamics_field": "number", "required_if_present": False}
    ]

    field_mappings = {
        "creditMemoNumber": "vendorCreditMemoNumber",
        "dueDate": "dueDate",
        "issueDate": "creditMemoDate",
        "postingDate": "postingDate",
        "vendorReturnReasonId": "vendorReturnReasonId",
        "invoiceId": "invoiceId",
        "invoiceNumber": "invoiceNumber",
    }

    def to_dynamics(self) -> dict:
        self._validate_company()

        if "creditMemoNumber" in self.record and self.record["creditMemoNumber"] is not None:
            self.record["creditMemoNumber"] = str(self.record["creditMemoNumber"])

        payload = {
            **self._map_internal_id(),
            **self._map_vendor(required=True),
            **self._map_currency(),
            **self._map_dimension_set_lines()
        }

        self._map_fields(payload)

        if self.sink.dynamics_client.custom_api_supports_credit_memos:
            # The custom Precoro API names this field differently than the standard BC API
            if "vendorCreditMemoNumber" in payload:
                payload["vendorCrMemoNumber"] = payload.pop("vendorCreditMemoNumber")
            # whtTaxCode only exists on the custom API entity, never on the standard one
            wht_tax_code = self.record.get("whtTaxCode")
            if wht_tax_code is not None and self.sink.dynamics_client.custom_api_entity_has_field("purchaseCreditMemo", "whtTaxCode"):
                payload["whtTaxCode"] = wht_tax_code

        self._map_credit_memo_line_items(payload)
        self._map_attachments(payload)

        return {"payload": payload, "company_id": self.company["id"]}

    def _map_credit_memo_line_items(self, payload):
        mapped_line_items = []
        existing_lines = self.existing_record.get("purchaseCreditMemoLines", []) if self.existing_record else []

        line_items = self.record.get("lineItems", [])
        for line_item in line_items:
            line_item["subsidiaryId"] = self.company["id"]
            line_payload = CreditMemoLineItemSchemaMapper(line_item, self.sink, self.reference_data, existing_lines).to_netsuite()
            mapped_line_items.append(line_payload)

        expense_items = self.record.get("expenses", [])
        for expense_item in expense_items:
            expense_item["subsidiaryId"] = self.company["id"]
            expense_line_payload = CreditMemoExpenseItemSchemaMapper(expense_item, self.sink, self.reference_data, existing_lines).to_netsuite()
            mapped_line_items.append(expense_line_payload)

        comments = self.record.get("comments", [])
        for comment in comments:
            comment_payload = BillCommentSchemaMapper(comment, self.sink, self.reference_data, existing_lines).to_netsuite()
            mapped_line_items.append(comment_payload)

        if mapped_line_items:
            payload["purchaseCreditMemoLines"] = mapped_line_items

    def _map_attachments(self, payload):
        attachments = self.record.get("attachments", [])

        mapped_attachments = []
        for attachment in attachments:
            attachment_payload = AttachmentSchemaMapper({
                "fileName": attachment,
                "parentId": payload.get("id"),
                "parentType": "Purchase Credit Memo",
                "subsidiaryId": self.company["id"]
            }, self.sink, self.reference_data).to_dynamics()
            mapped_attachments.append(attachment_payload)

        if mapped_attachments:
            payload["attachments"] = mapped_attachments
