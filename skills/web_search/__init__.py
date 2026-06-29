import skills
from .web_search_skill import WebSearchSkill
from .providers import tavily_provider, brave_provider, serper_provider, exa_provider, searxng_provider

web_search_skill_instance = WebSearchSkill()
skills.register(web_search_skill_instance)
