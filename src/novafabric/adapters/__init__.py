from novafabric.adapters.git import get_head_sha
from novafabric.adapters.langfuse import resolve_prompt_uri
from novafabric.adapters.mlflow import validate_run_id

# Framework adapters — lazy imports; each raises ImportError at call time
# if the target framework is not installed.
# Usage:
#   from novafabric.adapters.langgraph import wrap
#   from novafabric.adapters.autogen import wrap_agent
#   from novafabric.adapters.crewai import wrap_crew
#   from novafabric.adapters.dspy import wrap_program
#   from novafabric.adapters.llamaindex import wrap_engine
#   from novafabric.adapters.pydantic_ai import wrap_agent
#   from novafabric.adapters.haystack import wrap_pipeline

__all__ = [
    "get_head_sha",
    "resolve_prompt_uri",
    "validate_run_id",
    # Framework adapters (importable by name, lazy at call time):
    "wrap_langgraph",
    "wrap_autogen",
    "wrap_crewai",
    "wrap_dspy",
    "register_openai_agents",
    "make_google_adk_plugin",
    "wrap_bedrock_agentcore",
    "make_a2a_interceptor",
    "wrap_llamaindex",
    "wrap_pydantic_ai",
    "wrap_haystack",
]


def wrap_langgraph(graph, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.langgraph.wrap``."""
    from novafabric.adapters.langgraph import wrap
    return wrap(graph, **kwargs)


def wrap_autogen(agent, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.autogen.wrap_agent``."""
    from novafabric.adapters.autogen import wrap_agent
    return wrap_agent(agent, **kwargs)


def wrap_crewai(crew, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.crewai.wrap_crew``."""
    from novafabric.adapters.crewai import wrap_crew
    return wrap_crew(crew, **kwargs)


def wrap_dspy(program, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.dspy.wrap_program``."""
    from novafabric.adapters.dspy import wrap_program
    return wrap_program(program, **kwargs)


def register_openai_agents(**kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.openai_agents.register``."""
    from novafabric.adapters.openai_agents import register
    return register(**kwargs)


def make_google_adk_plugin(**kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.google_adk.make_plugin``."""
    from novafabric.adapters.google_adk import make_plugin
    return make_plugin(**kwargs)


def wrap_bedrock_agentcore(client, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.bedrock_agentcore.wrap_client``."""
    from novafabric.adapters.bedrock_agentcore import wrap_client
    return wrap_client(client, **kwargs)


def make_a2a_interceptor(**kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.a2a.make_interceptor``."""
    from novafabric.adapters.a2a import make_interceptor
    return make_interceptor(**kwargs)


def wrap_llamaindex(engine, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.llamaindex.wrap_engine``."""
    from novafabric.adapters.llamaindex import wrap_engine
    return wrap_engine(engine, **kwargs)


def wrap_pydantic_ai(agent, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.pydantic_ai.wrap_agent``."""
    from novafabric.adapters.pydantic_ai import wrap_agent
    return wrap_agent(agent, **kwargs)


def wrap_haystack(pipeline, **kwargs):  # type: ignore[no-untyped-def]
    """Alias: ``novafabric.adapters.haystack.wrap_pipeline``."""
    from novafabric.adapters.haystack import wrap_pipeline
    return wrap_pipeline(pipeline, **kwargs)
