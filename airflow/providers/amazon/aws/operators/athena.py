#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any, Sequence

from airflow.configuration import conf
from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.providers.amazon.aws.hooks.athena import AthenaHook
from airflow.providers.amazon.aws.triggers.athena import AthenaTrigger

if TYPE_CHECKING:
    from airflow.utils.context import Context


class AthenaOperator(BaseOperator):
    """
    An operator that submits a presto query to athena.

    .. note:: if the task is killed while it runs, it'll cancel the athena query that was launched,
        EXCEPT if running in deferrable mode.

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:AthenaOperator`

    :param query: Presto to be run on athena. (templated)
    :param database: Database to select. (templated)
    :param catalog: Catalog to select. (templated)
    :param output_location: s3 path to write the query results into. (templated)
    :param aws_conn_id: aws connection to use
    :param client_request_token: Unique token created by user to avoid multiple executions of same query
    :param workgroup: Athena workgroup in which query will be run. (templated)
    :param query_execution_context: Context in which query need to be run
    :param result_configuration: Dict with path to store results in and config related to encryption
    :param sleep_time: Time (in seconds) to wait between two consecutive calls to check query status on Athena
    :param max_polling_attempts: Number of times to poll for query state before function exits
        To limit task execution time, use execution_timeout.
    :param log_query: Whether to log athena query and other execution params when it's executed.
        Defaults to *True*.
    """

    ui_color = "#44b5e2"
    template_fields: Sequence[str] = ("query", "database", "output_location", "workgroup", "catalog")
    template_ext: Sequence[str] = (".sql",)
    template_fields_renderers = {"query": "sql"}

    def __init__(
        self,
        *,
        query: str,
        database: str,
        output_location: str,
        aws_conn_id: str = "aws_default",
        client_request_token: str | None = None,
        workgroup: str = "primary",
        query_execution_context: dict[str, str] | None = None,
        result_configuration: dict[str, Any] | None = None,
        sleep_time: int = 30,
        max_polling_attempts: int | None = None,
        log_query: bool = True,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        catalog: str = "AwsDataCatalog",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.query = query
        self.database = database
        self.output_location = output_location
        self.aws_conn_id = aws_conn_id
        self.client_request_token = client_request_token
        self.workgroup = workgroup
        self.query_execution_context = query_execution_context or {}
        self.result_configuration = result_configuration or {}
        self.sleep_time = sleep_time
        self.max_polling_attempts = max_polling_attempts or 999999
        self.query_execution_id: str | None = None
        self.log_query: bool = log_query
        self.deferrable = deferrable
        self.catalog: str = catalog

    @cached_property
    def hook(self) -> AthenaHook:
        """Create and return an AthenaHook."""
        return AthenaHook(self.aws_conn_id, log_query=self.log_query)

    def execute(self, context: Context) -> str | None:
        """Run Presto Query on Athena."""
        self.query_execution_context["Database"] = self.database
        self.query_execution_context["Catalog"] = self.catalog
        self.result_configuration["OutputLocation"] = self.output_location
        self.query_execution_id = self.hook.run_query(
            self.query,
            self.query_execution_context,
            self.result_configuration,
            self.client_request_token,
            self.workgroup,
        )

        if self.deferrable:
            self.defer(
                trigger=AthenaTrigger(
                    self.query_execution_id, self.sleep_time, self.max_polling_attempts, self.aws_conn_id
                ),
                method_name="execute_complete",
            )
        # implicit else:
        query_status = self.hook.poll_query_status(
            self.query_execution_id,
            max_polling_attempts=self.max_polling_attempts,
            sleep_time=self.sleep_time,
        )

        if query_status in AthenaHook.FAILURE_STATES:
            error_message = self.hook.get_state_change_reason(self.query_execution_id)
            raise Exception(
                f"Final state of Athena job is {query_status}, query_execution_id is "
                f"{self.query_execution_id}. Error: {error_message}"
            )
        elif not query_status or query_status in AthenaHook.INTERMEDIATE_STATES:
            raise Exception(
                f"Final state of Athena job is {query_status}. Max tries of poll status exceeded, "
                f"query_execution_id is {self.query_execution_id}."
            )

        return self.query_execution_id

    def execute_complete(self, context, event=None):
        if event["status"] != "success":
            raise AirflowException(f"Error while waiting for operation on cluster to complete: {event}")
        return event["value"]

    def on_kill(self) -> None:
        """Cancel the submitted athena query."""
        if self.query_execution_id:
            self.log.info("Received a kill signal.")
            response = self.hook.stop_query(self.query_execution_id)
            http_status_code = None
            try:
                http_status_code = response["ResponseMetadata"]["HTTPStatusCode"]
            except Exception:
                self.log.exception(
                    "Exception while cancelling query. Query execution id: %s", self.query_execution_id
                )
            finally:
                if http_status_code is None or http_status_code != 200:
                    self.log.error("Unable to request query cancel on athena. Exiting")
                else:
                    self.log.info(
                        "Polling Athena for query with id %s to reach final state", self.query_execution_id
                    )
                    self.hook.poll_query_status(self.query_execution_id, sleep_time=self.sleep_time)
