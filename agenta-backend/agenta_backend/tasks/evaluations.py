from collections import defaultdict
from bson import ObjectId
from celery import shared_task
import asyncio
from datetime import datetime

from agenta_backend.services import llm_apps_service
from agenta_backend.services.db_manager import (
    create_new_evaluation,
    fetch_app_variant_by_id,
    get_deployment_by_objectid,
    fetch_testset_by_id,
    create_new_evaluation_scenario,
    update_evaluation_with_aggregated_results,
)
from agenta_backend.models.api.evaluation_model import NewEvaluation

from agenta_backend.models.db_models import (
    AggregatedResult,
    AppDB,
    EvaluationScenarioOutputDB,
    EvaluationScenarioResult,
    EvaluatorConfigDB,
    Result,
)

from agenta_backend.services import evaluators_service


@shared_task(queue="agenta_backend.tasks.evaluations.evaluate")
def evaluate(app_data, new_evaluation_data):
    loop = asyncio.get_event_loop()
    new_evaluation = NewEvaluation(**new_evaluation_data)
    app = AppDB(**app_data)
    testset = loop.run_until_complete(fetch_testset_by_id(new_evaluation.testset_id))

    new_evaluation_db = loop.run_until_complete(
        create_new_evaluation(
            app=app,
            organization=app.organization,
            user=app.user,
            testset=testset,
            variants=new_evaluation.variant_ids,
            evaluators_configs=new_evaluation.evaluators_configs,
        )
    )
    evaluators_aggregated_data = defaultdict(list)

    for variant_id in new_evaluation.variant_ids:
        variant_id = str(variant_id)
        app_variant_db = loop.run_until_complete(fetch_app_variant_by_id(variant_id))
        deployment = loop.run_until_complete(
            get_deployment_by_objectid(app_variant_db.base.deployment)
        )

        # TODO: remove if abraham's fix is working
        uri = deployment.uri.replace("http://localhost", "http://host.docker.internal")

        for data_point in testset.csvdata:
            variant_output = llm_apps_service.get_llm_app_output(uri, data_point)

            evaluators_results: [EvaluationScenarioResult] = []
            for evaluator_config in new_evaluation.evaluators_configs:
                result = evaluators_service.evaluate(
                    evaluator_config.evaluator_key,
                    data_point["correct_answer"],
                    variant_output,
                )

                result_object = EvaluationScenarioResult(
                    evaluator_key=evaluator_config.evaluator_key,
                    result=Result(
                        type="number",
                        value=result
                    ),
                )
                evaluators_results.append(result_object)
                evaluators_aggregated_data[evaluator_config.evaluator_key].append(
                    result
                )

            evaluation_scenario = loop.run_until_complete(
                create_new_evaluation_scenario(
                    user=app.user,
                    organization=app.organization,
                    evaluation=new_evaluation_db,
                    evaluators_configs=new_evaluation_db.evaluators_configs,
                    inputs=[],
                    is_pinned=False,
                    note="",
                    correct_answer=data_point["correct_answer"],
                    outputs=[
                        EvaluationScenarioOutputDB(type="text", value=variant_output)
                    ],
                    results=evaluators_results,
                )
            )

    aggregated_results = aggregate_evaluator_results(evaluators_aggregated_data)
    updated_evaluation = loop.run_until_complete(update_evaluation_with_aggregated_results(new_evaluation_db.id, aggregated_results))

def aggregate_evaluator_results(evaluators_aggregated_data):
    aggregated_results = []
    for evaluator_key, values in evaluators_aggregated_data.items():
        average_value = sum(values) / len(values) if values else 0
        aggregated_result_value:AggregatedResult = AggregatedResult(
            evaluator_config=EvaluatorConfigDB(
                evaluator_key=evaluator_key
            ),
            result=Result(
                type="number",
                value=str(average_value)
            )
        )
        aggregated_results.append(aggregated_result_value)
    return aggregated_results