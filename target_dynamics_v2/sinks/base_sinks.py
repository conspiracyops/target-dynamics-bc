import abc
import hashlib
import json
from typing import Dict, List, Optional, Any

from singer_sdk.plugin_base import PluginBase
from singer_sdk.sinks import BatchSink
from target_hotglue.client import HotglueBaseSink
from target_hotglue.common import HGJSONEncoder

from target_dynamics_v2.client import DynamicsClient

class DynamicsBaseBatchSink(HotglueBaseSink, BatchSink):
    max_size = 1000 # max allowed by dynamics is 1000

    def __init__(
        self,
        target: PluginBase,
        stream_name: str,
        schema: Dict,
        key_properties: Optional[List[str]],
    ) -> None:
        super().__init__(target, stream_name, schema, key_properties)
        self.dynamics_client: DynamicsClient = self._target.dynamics_client

    @abc.abstractmethod
    def preprocess_batch(self, records: List[dict]):
        """
        Can be used to gather any additional data before processing the batch
        such as making a bulk request to get all existing records based on the
        given "records".
        """
        pass

    @abc.abstractmethod
    def process_batch_record(self, record: dict, index: int) -> dict:
        """
        Process the record. Do the raw record mapping to what will be used to perform
        the requests against the API
        """
        return record

    def build_record_hash(self, record: dict):
        return hashlib.sha256(json.dumps(record, cls=HGJSONEncoder).encode()).hexdigest()

    def hash_records(self, records: List[dict]):
        for record in records:
            record["hash"] = self.build_record_hash(record)

    def get_existing_state(self, hash: str):
        states = self.latest_state["bookmarks"][self.name]

        existing_state = next((s for s in states if hash==s.get("hash") and s.get("success")), None)

        if existing_state:
            self.latest_state["summary"][self.name]["existing"] += 1

        return existing_state
    
    def check_for_duplicated_records(self, records: List[dict]):
        filtered_records = []

        # filter out duplicated records from previous batches
        for record in records:
            hash = record["hash"]
            existing_state = self.get_existing_state(hash)

            if existing_state:
                self.logger.info(f"Duplicated record. Won't process it. Record: {record}")
                continue
            filtered_records.append(record)

        # filter out duplicated records within the same batch
        seen_hashes = set()
        unique_records = []
        for record in filtered_records:
            if record["hash"] not in seen_hashes:
                seen_hashes.add(record["hash"])
                unique_records.append(record)
            else:
                self.logger.info(f"Duplicated record. Won't process it. Record: {record}")

        return unique_records
    
    def error_to_string(self, error: Any):
        if isinstance(error, dict) and "message" in error:
            return error.get("message")
        else:
            return str(error)

    def build_state_from_response(self, response: dict) -> dict:
        state = {
            "id": None,
            "success": False,
            "is_updated": False,
        }

        if response["status"] in [200, 201]:
            state["success"] = True
            state["id"] = response.get("body", {}).get("id")

        if response["status"] == 200:
            state["is_updated"] = True

        if response["status"] >= 400:
            state["success"] = False
            state["error"] = response.get("body", {}).get("error")

        return state

    # Streams whose successful upserts should be registered in the
    # Precoro AccountSetup mapping microservice.
    ACCOUNT_SETUP_STREAMS = {"Vendors"}

    @property
    def account_setup_client(self):
        if not hasattr(self, "_account_setup_client"):
            from target_dynamics_v2.account_setup import AccountSetupClient
            self._account_setup_client = AccountSetupClient(
                getattr(self._target, "account_setup", {}), self.logger
            )
        return self._account_setup_client

    def _resolve_legal_entity_id(self, company_id):
        """Reverse-map a BC company GUID to the Precoro legalEntityId.

        AccountSetup.legalEntity is {precoroLegalEntityId: dynamicsCompanyGuid}.
        """
        legal_entity_map = (getattr(self._target, "account_setup", {}) or {}).get("legalEntity", {})
        for legal_entity_id, company_guid in legal_entity_map.items():
            if str(company_guid) == str(company_id):
                return legal_entity_id
        return None

    def register_account_setup_mapping(self, state: dict) -> None:
        """Register the Precoro<->BC id mapping for a successful upsert.

        Never raises: a mapping failure must not fail the export itself.
        """
        if self.name not in self.ACCOUNT_SETUP_STREAMS or not state.get("success"):
            return

        client = self.account_setup_client
        if not client.enabled:
            return

        integration_id = state.get("id")          # the BC record id
        entity_id = state.get("externalId")        # the Precoro entity id
        if not integration_id or not entity_id:
            return

        legal_entity_id = self._resolve_legal_entity_id(state.get("companyId"))
        if legal_entity_id is None:
            self.logger.warning(
                f"Skipping AccountSetup mapping for {self.name} externalId={entity_id}: "
                f"could not resolve legalEntityId for companyId={state.get('companyId')}"
            )
            return

        try:
            response = client.register_mapping(
                integration_id=integration_id,
                precoro_id=entity_id,
                legal_entity_id=legal_entity_id,
                entity_type=client.entity_type_for(self.name),
                name=state.get("name"),
                map_field=state.get("mapField"),
            )
            self.logger.info(f"AccountSetup mapping registered for {self.name} externalId={entity_id}: {response}")
        except Exception as exc:
            self.logger.error(f"AccountSetup mapping registration failed for {self.name} externalId={entity_id}: {exc}")

class DynamicsBaseBatchSinkBatchUpsert(DynamicsBaseBatchSink):
    """
    Sink that batch records for pre-processing and also make the
    upsert requests to Dynamics in batches
    """

    def make_batch_request(self, records: List[dict], transaction_type: str = "non_atomic"):
        if not records:
            return []

        responses = []
        for record in records:
            requests_data = []
            for request in record["records"]:
                data = {
                    "method": request["request_params"]["method"],
                    "url": request["request_params"]["url"],
                    "headers": {
                        **request["request_params"].get("headers", {})
                    },
                    "body": request["payload"]
                }
                requests_data.append(data)

            if requests_data:
                responses += self.dynamics_client.make_batch_request(requests_data, transaction_type=transaction_type)

        return responses

    def handle_non_atomic_batch_response(self, responses: List[dict], records: List[dict], raw_records: List[dict]) -> dict:
        """
        This method should return a dict.
        It's recommended that you return a key named "state_updates".
        This key should be an array of all state updates.
        
        responses: a list of responses from the API
        records: a list of records used to make the request to the API
        
        responses and records have the same order, so we can relate a record to a response
        """
        state_updates = []

        for index, response in enumerate(responses):
            state = self.build_state_from_response(response)

            record = records[index]
            raw_record = raw_records[record["raw_record_index"]]
            if external_id := raw_record.get("externalId"):
                state["externalId"] = external_id
            state.update(record.get("state_fields", {}))

            if response["status"] >= 400:
                state["success"] = False
                state["record"] = json.dumps(record["records"], cls=HGJSONEncoder, sort_keys=True)

            self.register_account_setup_mapping(state)
            state_updates.append(state)

        return {"state_updates": state_updates}

    def handle_atomic_batch_response(self, responses: List[dict], record: dict, raw_records: List[dict]) -> dict:
        """
        This method should return a dict with the state update
        
        for the atomic batch request all the requests are related to one entity
        if one fails it will stop executing, so we check the last response code
        if it's an error we return an error state.
        if it's success we look for the code in the first response (which is the
        response for the main entity)

        responses: a list of responses from the API
        record: used to make the requests to the API
        """
        first_response = responses[0]
        last_response = responses[-1]
        response_for_state = last_response if last_response["status"] >= 400 else first_response
        state = self.build_state_from_response(response_for_state)
        state["responses"] = responses

        raw_record = raw_records[record["raw_record_index"]]
        if external_id := raw_record.get("externalId"):
            state["externalId"] = external_id
        state.update(record.get("state_fields", {}))

        if last_response["status"] >= 400:
            state["record"] = json.dumps(record["records"], cls=HGJSONEncoder, sort_keys=True)
            return state

        self.register_account_setup_mapping(state)
        return state
    
    def process_batch(self, context: dict) -> None:
        if not self.latest_state:
            self.init_state()

        raw_records = context["records"]

        self.preprocess_batch(raw_records)

        records = []
        for index, raw_record in enumerate(raw_records):
            try:
                record_hash = self.build_record_hash(raw_record)
                # if the record is duplicated within this job run we skip it
                if self.get_existing_state(record_hash):
                    continue
                # performs record mapping from unified to Dynamics
                record = self.process_batch_record(raw_record)
                record["raw_record_index"] = index
                records.append(record)
            except Exception as e:
                self.logger.exception(e)
                state = {"success": False, "error": str(e), "record": json.dumps(raw_record, cls=HGJSONEncoder, sort_keys=True)}
                if id := raw_record.get("id"):
                    state["id"] = id
                if external_id := raw_record.get("externalId"):
                    state["externalId"] = external_id

                self.update_state(state)

        self.hash_records(records)
        records = self.check_for_duplicated_records(records)

        # separate atomic and non atomic records
        # 
        # non atomic records are records that just need one API operation, we bulk
        # all of them in one single batch operation
        # 
        # atomic records are records that need to perform more than one API operation,
        # for example updating a customer which also update it's default dimensions,
        # then all the requests for that given customer is performed in one transactional batch
        # operation, in case one of the operation fails the others are automatically rolledback
        # to keep record consistency
        atomic_records = [record for record in records if len(record["records"])>1]
        non_atomic_records = [record for record in records if len(record["records"])==1] 

        non_atomic_responses = self.make_batch_request(non_atomic_records)
        result = self.handle_non_atomic_batch_response(non_atomic_responses, non_atomic_records, raw_records)
        for state, record in zip(result.get("state_updates", list()), non_atomic_records):
            self.update_state(state, record=record)

        for atomic_record, index in atomic_records:
            atomic_responses = self.make_batch_request([atomic_record], transaction_type="atomic")
            state = self.handle_atomic_batch_response(atomic_responses, atomic_record, raw_records)
            self.update_state(state)


class DynamicsBaseBatchSinkSingleUpsert(DynamicsBaseBatchSink):
    """
    Sink that batch records for pre-processing but makes the
    upsert requests to Dynamics one at a time. This is used
    for sinks that need more complex logic to upsert records
    in Dynamics.
    For example when creating the record and the
    child records needs the parent ID that has just been created
    """

    @abc.abstractmethod
    def upsert_record(self, record: Dict) -> tuple[str, bool, Dict]:
        """
        Performs the upserting of the record in Dynamics
        """
        pass

    def process_batch(self, context: dict) -> None:
        if not self.latest_state:
            self.init_state()

        raw_records = context["records"]

        self.preprocess_batch(raw_records)

        records = []
        for raw_record in raw_records:
            try:
                # performs record mapping from unified to Dynamics
                record = self.process_batch_record(raw_record)
                record["externalId"] = raw_record.get("externalId")
                records.append(record)
            except Exception as e:
                self.logger.exception(e)
                state = {"error": str(e), "record": json.dumps(raw_record, cls=HGJSONEncoder, sort_keys=True)}
                if id := raw_record.get("id"):
                    state["id"] = id
                if external_id := raw_record.get("externalId"):
                    state["externalId"] = external_id
                self.update_state(state)

        self.hash_records(records)
        records = self.check_for_duplicated_records(records)

        for record in records:
            try:
                id, success, state = self.upsert_record(record)
            except  Exception as e:
                state = {"error": str(e), "record": json.dumps(record, cls=HGJSONEncoder, sort_keys=True)}
                self.update_state(state)
            else:
                if success:
                    self.logger.info(f"{self.name} processed id: {id}")

                state["success"] = success
                state["hash"] = record.get("hash")

                if id:
                    state["id"] = id

                self.update_state(state)
