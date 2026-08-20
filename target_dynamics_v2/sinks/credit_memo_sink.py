import json
import singer
from typing import Dict, List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.mappers.credit_memo_schema_mapper import CreditMemoSchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkSingleUpsert
from target_hotglue.common import HGJSONEncoder

LOGGER = singer.get_logger()


class CreditMemoSink(DynamicsBaseBatchSinkSingleUpsert):
    name = "CreditMemos"
    record_type = "purchaseCreditMemos"

    def preprocess_batch(self, records: List[dict]):
        # fetch reference data related to existing credit memos
        credit_memo_filter_mappings = [
            {"field_from": "id", "field_to": "id", "should_quote": False},
            {"field_from": "transactionNumber", "field_to": "number", "should_quote": True},
        ]
        existing_company_credit_memos = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            credit_memo_filter_mappings,
            expand="dimensionSetLines, purchaseCreditMemoLines($expand=dimensionSetLines), attachments"
        )

        # get vendors for company, filter by id, number, displayName
        vendor_filter_mappings = [
            {"field_from": "vendorId", "field_to": "id", "should_quote": False},
            {"field_from": "vendorExternalId", "field_to": "number", "should_quote": True},
            {"field_from": "vendorName", "field_to": "displayName", "should_quote": True},
        ]
        existing_company_vendors = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            "Vendors",
            records,
            vendor_filter_mappings
        )

        # get items
        items_set = set()
        for record in records:
            items_set.update((line_item.get("itemId"), line_item.get("itemExternalId"), line_item.get("itemName"), record.get("subsidiaryId"), record.get("subsidiaryName")) for line_item in record.get("lineItems", []))
        items = [{"itemId": item[0], "itemExternalId": item[1], "itemName": item[2], "subsidiaryId": item[3], "subsidiaryName": item[4]} for item in items_set]
        sorted_items = sorted(items, key=lambda item: (item.get("itemId"), item.get("itemExternalId"), item.get("itemName"), item.get("subsidiaryId"), item.get("subsidiaryName")))
        item_filter_mappings = [
            {"field_from": "itemId", "field_to": "id", "should_quote": False},
            {"field_from": "itemExternalId", "field_to": "number", "should_quote": True},
            {"field_from": "itemName", "field_to": "displayName", "should_quote": True},
        ]
        existing_company_items = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            "Items",
            sorted_items,
            item_filter_mappings
        ) if items else []

        self.reference_data = {**self._target.reference_data, self.name: existing_company_credit_memos, "Vendors": existing_company_vendors, "Items": existing_company_items}

    def process_batch_record(self, record: dict) -> dict:
        return CreditMemoSchemaMapper(record, self, self.reference_data).to_dynamics()

    def upsert_record(self, record: Dict) -> tuple[str, bool, Dict]:
        state = {}
        externalId = record.pop("externalId", None)
        payload = record["payload"]

        if externalId:
            state["externalId"] = externalId

        company_id = record["company_id"]
        credit_memo_id = payload.pop("id", None)
        is_update = credit_memo_id is not None
        credit_memo_dimensions = payload.pop("dimensionSetLines", [])
        credit_memo_lines = payload.pop("purchaseCreditMemoLines", [])
        attachments = payload.pop("attachments", [])

        # create/update credit memo
        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, company_id, credit_memo_id)
        credit_memo_upsert_request_data = [{ **request_params, "body": payload }]
        credit_memo_upsert_response = self.dynamics_client.make_batch_request(credit_memo_upsert_request_data)[0]

        if credit_memo_upsert_response.get("status") not in [200, 201]:
            error = credit_memo_upsert_response.get("body", {}).get("error", {})
            error_message = error.get("message", "") if isinstance(error, dict) else str(error)
            # The custom Precoro API only exposes unposted credit memos, so a PATCH on an id
            # that preprocess found via the standard API returns 404 when the memo is posted.
            is_posted = "posted and can no longer be modified" in error_message
            is_gone_from_custom_api = (
                credit_memo_upsert_response.get("status") == 404
                or "does not exist" in error_message.lower()
                or "not found" in error_message.lower()
            )
            if is_update and (is_posted or is_gone_from_custom_api):
                LOGGER.warning(
                    f"Credit memo {credit_memo_id} is already posted in Dynamics BC and cannot be modified. Skipping update."
                )
                state["is_updated"] = True
                state["skipped"] = True
                return credit_memo_id, True, state
            state["error"] = error
            state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
            return None, False, state

        credit_memo_id = credit_memo_upsert_response["body"]["id"]

        # re-fetch to get inherited dimensionSetLines from the Vendor
        _, _, credit_memos = self.dynamics_client.get_entities(self.record_type, url_params={"companyId": company_id}, filters={"id": [credit_memo_id]}, expand="dimensionSetLines")
        upserted_credit_memo = credit_memos[0]

        # create/update credit memo dimensions
        if credit_memo_dimensions:
            existing_credit_memo_dimensions = upserted_credit_memo.get("dimensionSetLines", [])
            credit_memo_dimensions_requests = DynamicsClient.create_dimension_set_lines_requests("purchaseCreditMemosDimensionSetLines", company_id, credit_memo_id, credit_memo_dimensions, existing_credit_memo_dimensions)
            credit_memo_dimensions_upsert_responses = self.dynamics_client.make_batch_request(credit_memo_dimensions_requests)

            for credit_memo_dimensions_upsert_response in credit_memo_dimensions_upsert_responses:
                if credit_memo_dimensions_upsert_response["status"] not in [200, 201]:
                    state["error"] = credit_memo_dimensions_upsert_response.get("body", {}).get("error")
                    state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
                    if not is_update:
                        self.delete_credit_memo(credit_memo_id, company_id)
                    return None, False, state

        credit_memo_lines_dimensions = []
        if credit_memo_lines:
            # create/update lines
            credit_memo_lines_upsert_request_data = []
            url_params = {"parentId": credit_memo_id}
            for index, credit_memo_line in enumerate(credit_memo_lines):
                credit_memo_line_id = credit_memo_line.pop("id", None)
                request_id = credit_memo_line_id or f"temp_{index}"
                if credit_memo_line_dimensions := credit_memo_line.pop("dimensionSetLines", []):
                    credit_memo_lines_dimensions.append({
                        "request_id": request_id,
                        "dimension_set_lines": credit_memo_line_dimensions
                    })

                request_params = DynamicsClient.get_entity_upsert_request_params("purchaseCreditMemoLines", company_id, entity_id=credit_memo_line_id, url_params=url_params, request_id=request_id)
                credit_memo_lines_upsert_request_data.append({ **request_params, "body": credit_memo_line })

            credit_memo_lines_upsert_responses = self.dynamics_client.make_batch_request(credit_memo_lines_upsert_request_data) if credit_memo_lines_upsert_request_data else []
            for credit_memo_lines_upsert_response in credit_memo_lines_upsert_responses:
                if credit_memo_lines_upsert_response.get("status") not in [200, 201]:
                    state["error"] = credit_memo_lines_upsert_response.get("body", {}).get("error")
                    state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
                    if not is_update:
                        self.delete_credit_memo(credit_memo_id, company_id)
                    return None, False, state

        if credit_memo_lines_dimensions:
            # re-fetch to get inherited dimensionSetLines
            _, _, credit_memos = self.dynamics_client.get_entities(self.record_type, url_params={"companyId": company_id}, filters={"id": [credit_memo_id]}, expand="dimensionSetLines, purchaseCreditMemoLines($expand=dimensionSetLines)")
            upserted_credit_memo = credit_memos[0]

            # create/update lines dimensions
            credit_memo_lines_dimensions_requests = []
            for credit_memo_line_dimensions in credit_memo_lines_dimensions:
                request_id = credit_memo_line_dimensions["request_id"]
                credit_memo_line_dimension_set_lines = credit_memo_line_dimensions["dimension_set_lines"]

                if not credit_memo_line_dimension_set_lines:
                    continue

                credit_memo_line_id = next(credit_memo_line_upsert_response["body"]["id"] for credit_memo_line_upsert_response in credit_memo_lines_upsert_responses if credit_memo_line_upsert_response["id"] == request_id)

                upserted_credit_memo_line = next((credit_memo_line for credit_memo_line in upserted_credit_memo["purchaseCreditMemoLines"] if credit_memo_line["id"] == credit_memo_line_id), None)
                existing_credit_memo_line_dimensions = upserted_credit_memo_line.get("dimensionSetLines", [])

                credit_memo_lines_dimensions_requests += DynamicsClient.create_dimension_set_lines_requests("purchaseCreditMemoLinesDimensionSetLines", company_id, credit_memo_line_id, credit_memo_line_dimension_set_lines, existing_credit_memo_line_dimensions)

            credit_memo_lines_dimensions_upsert_responses = self.dynamics_client.make_batch_request(credit_memo_lines_dimensions_requests)

            for credit_memo_lines_dimensions_upsert_response in credit_memo_lines_dimensions_upsert_responses:
                if credit_memo_lines_dimensions_upsert_response["status"] not in [200, 201]:
                    state["error"] = credit_memo_lines_dimensions_upsert_response.get("body", {}).get("error")
                    state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
                    if not is_update:
                        self.delete_credit_memo(credit_memo_id, company_id)
                    return None, False, state

        if attachments:
            attachments_requests = []
            request_params = DynamicsClient.get_entity_upsert_request_params("Attachments", company_id, url_params={"parentId": credit_memo_id})
            new_attachments = [a for a in attachments if not a.get("payload", {}).get("id")]
            identified_attachments = [a for a in attachments if a.get("payload", {}).get("id")]
            for attachment in new_attachments:
                attachments_requests.append(
                    { **request_params,
                     "body": {
                         "parentId": credit_memo_id,
                         "fileName": attachment.get("payload", {}).get("fileName"),
                         "parentType": "Purchase Credit Memo",
                         }
                    }
                )
            attachments_post_responses = self.dynamics_client.make_batch_request(attachments_requests)
            for attachments_post_response, attachment in zip(attachments_post_responses, new_attachments):
                if attachments_post_response["status"] not in [200, 201]:
                    state["error"] = attachments_post_response.get("body", {}).get("error")
                    state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
                    if not is_update:
                        self.delete_credit_memo(credit_memo_id, company_id)
                    return None, False, state
                attachment["payload"]["id"] = attachments_post_response["body"]["id"]
                identified_attachments.append(attachment)

            # Now we patch the attachment content
            for attachment in identified_attachments:
                attachment_id = attachment.get("payload", {}).get("id")
                attachment_content = attachment.get("payload", {}).get("attachmentContent")
                request_params = DynamicsClient.get_entity_upsert_request_params("AttachmentsContent", company_id, url_params={"parentId": attachment_id})
                request_params["headers"] = {"Content-Type": "application/octet-stream", "If-Match": "*"}
                response = self.dynamics_client._make_request(request_params["url"], "PATCH", data=attachment_content, headers=request_params["headers"], should_dump_json=False)
                if response.status_code not in [200, 201, 204]:
                    state["error"] = response.json().get("error") if response.text else "Unparsed error"
                    state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
                    if not is_update:
                        self.delete_credit_memo(credit_memo_id, company_id)
                    return None, False, state

        if is_update:
            state["is_updated"] = True

        return credit_memo_id, True, state

    def delete_credit_memo(self, credit_memo_id: str, company_id: str):
        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, company_id, credit_memo_id)
        credit_memo_delete_request_data = [{ **request_params, "method": "DELETE" }]
        self.dynamics_client.make_batch_request(credit_memo_delete_request_data)
