import json
from typing import Dict, List

from target_dynamics_v2.client import DynamicsClient
from target_dynamics_v2.mappers.journal_entry_schema_mapper import JournalEntrySchemaMapper
from target_dynamics_v2.sinks.base_sinks import DynamicsBaseBatchSinkSingleUpsert
from target_dynamics_v2.utils import DuplicatedRecord
from target_hotglue.common import HGJSONEncoder

class JournalEntrySink(DynamicsBaseBatchSinkSingleUpsert):
    name = "JournalEntries"
    record_type = "Journals"

    def preprocess_batch(self, records: List[Dict]):
        # fetch existing Journals
        filter_mappings = [
            {"field_from": "externalId", "field_to": "displayName", "should_quote": True}
        ]

        existing_company_journals = self.dynamics_client.get_existing_entities_for_records(
            self._target.reference_data.get("companies", []),
            self.record_type,
            records,
            filter_mappings
        )

        self.reference_data = {**self._target.reference_data, self.name: existing_company_journals}

    def process_batch_record(self, record: Dict) -> Dict:
        # perform the mapping
        return JournalEntrySchemaMapper(record, self, self.reference_data).to_dynamics()
    
    def upsert_record(self, record: Dict) -> tuple[str, bool, Dict]:
        state = {}

        payload = record["payload"]

        if existing_record_id := payload.get("id"):
            raise DuplicatedRecord(f"Found an existing Journal with id={existing_record_id}. Skipping it.")

        company_id = record.get("company_id")

        # create Journal and lines
        request_params = DynamicsClient.get_entity_upsert_request_params(self.record_type, company_id)
        journal_request_data = [
            {
                **request_params,
                "body": payload
            }
        ]
        journal_response = self.dynamics_client.make_batch_request(journal_request_data)[0]

        if journal_response.get("status") != 201:
            id = payload.get("code")
            state["error"] = journal_response.get("body", {}).get("error")
            state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
            return None, False, state
        
        journal_id = journal_response["body"]["id"]
        
        # if it's draft we don't need to do anything else
        if record["is_draft"]:
            return journal_id, True, state
        
        # if it's not draft we need to POST the journal and then delete it
        post_delete_request_data = [
            {
                "url": f"{request_params['url']}({journal_id})/Microsoft.NAV.post",
                "method": "POST",
                "body": {}
            },
            {
                "url": f"{request_params['url']}({journal_id})",
                "method": "DELETE",
                "body": {}
            }
        ]

        post_delete_response = self.dynamics_client.make_batch_request(post_delete_request_data, transaction_type="atomic")
        
        post_response = post_delete_response[0]
        if post_response.get("status") != 204:
            state["error"] = post_response.get("body", {}).get("error")
            state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
            return journal_id, False, state
        
        delete_response = post_delete_response[1]
        if delete_response.get("status") != 204:
            state["error"] = delete_response.get("body", {}).get("error")
            state["record"] = json.dumps(record, cls=HGJSONEncoder, sort_keys=True)
            return None, False, state

        return journal_id, True, state

