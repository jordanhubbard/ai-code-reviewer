#!/usr/bin/env python3
"""
Agent Spec Validator for AI Code Reviewer

Validates that agent configuration files conform to the Oracle Agent Spec
(https://oracle.github.io/agent-spec/26.1.0/).
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Try to import yaml, fall back gracefully
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    logger.warning("PyYAML not installed. YAML validation will not be available.")


@dataclass
class AgentValidation:
    """Result of agent spec validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    agent_name: Optional[str] = None
    agent_description: Optional[str] = None
    focus_areas: List[str] = field(default_factory=list)
    agentspec_version: Optional[str] = None

    def get_report(self) -> str:
        """Generate a human-readable validation report."""
        lines = []

        if self.is_valid:
            lines.append(f"[OK] Agent validated: {self.agent_name or 'unknown'}")
            if self.agentspec_version:
                lines.append(f"  Agent Spec version: {self.agentspec_version}")
            if self.agent_description:
                lines.append(f"  Description: {self.agent_description[:80]}...")
            if self.focus_areas:
                lines.append(f"  Focus areas: {', '.join(self.focus_areas)}")
        else:
            lines.append(f"[FAIL] Agent validation FAILED")

        if self.errors:
            lines.append("\nErrors:")
            for error in self.errors:
                lines.append(f"  - {error}")

        if self.warnings:
            lines.append("\nWarnings:")
            for warning in self.warnings:
                lines.append(f"  - {warning}")

        return "\n".join(lines)


class AgentSpecValidator:
    """Validates agent directory structure and agent.yaml/agent.json content."""

    # Required fields per Agent Spec
    REQUIRED_FIELDS = {
        "component_type": "Must be 'Agent'",
        "name": "Agent name is required",
        "system_prompt": "System prompt defines agent behavior",
    }

    # Recommended fields
    RECOMMENDED_FIELDS = {
        "description": "Agent description helps with discovery",
        "agentspec_version": "Version ensures compatibility",
        "inputs": "Inputs define what the agent accepts",
        "outputs": "Outputs define what the agent produces",
        "llm_config": "LLM configuration for the agent",
    }

    # Valid component types
    VALID_COMPONENT_TYPES = {"Agent", "SpecializedAgent", "RemoteAgent"}

    # Valid property types per JSON Schema
    VALID_PROPERTY_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}

    # Supported Agent Spec versions
    SUPPORTED_VERSIONS = {"25.4.1", "26.1.0"}

    @classmethod
    def validate_agent(cls, agent_dir: Path) -> AgentValidation:
        """
        Validate an agent directory.

        Args:
            agent_dir: Path to agent directory

        Returns:
            AgentValidation with results
        """
        errors = []
        warnings = []
        agent_name = agent_dir.name
        agent_description = None
        focus_areas = []
        agentspec_version = None

        # Check directory exists
        if not agent_dir.exists():
            return AgentValidation(
                is_valid=False,
                errors=[f"Agent directory does not exist: {agent_dir}"],
                warnings=[],
                agent_name=agent_name
            )

        if not agent_dir.is_dir():
            return AgentValidation(
                is_valid=False,
                errors=[f"Path is not a directory: {agent_dir}"],
                warnings=[],
                agent_name=agent_name
            )

        # Find agent spec file (agent.yaml or agent.json)
        agent_yaml = agent_dir / "agent.yaml"
        agent_json = agent_dir / "agent.json"

        agent_file = None
        agent_data = None

        if agent_yaml.exists():
            agent_file = agent_yaml
            if not YAML_AVAILABLE:
                errors.append("agent.yaml found but PyYAML is not installed. Install with: pip install pyyaml")
                return AgentValidation(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    agent_name=agent_name
                )
            try:
                with open(agent_yaml, 'r', encoding='utf-8') as f:
                    agent_data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                errors.append(f"Invalid YAML syntax in agent.yaml: {e}")
                return AgentValidation(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    agent_name=agent_name
                )
            except Exception as e:
                errors.append(f"Cannot read agent.yaml: {e}")
                return AgentValidation(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    agent_name=agent_name
                )

        elif agent_json.exists():
            agent_file = agent_json
            try:
                with open(agent_json, 'r', encoding='utf-8') as f:
                    agent_data = json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"Invalid JSON syntax in agent.json: {e}")
                return AgentValidation(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    agent_name=agent_name
                )
            except Exception as e:
                errors.append(f"Cannot read agent.json: {e}")
                return AgentValidation(
                    is_valid=False,
                    errors=errors,
                    warnings=warnings,
                    agent_name=agent_name
                )
        else:
            # Check for legacy format (AI_START_HERE.md)
            legacy_file = agent_dir / "AI_START_HERE.md"
            if legacy_file.exists():
                errors.append(
                    "Legacy persona format detected (AI_START_HERE.md). "
                    "Please migrate to Agent Spec format (agent.yaml or agent.json). "
                    "See: https://oracle.github.io/agent-spec/"
                )
            else:
                errors.append("Missing agent spec file: agent.yaml or agent.json")
            return AgentValidation(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                agent_name=agent_name
            )

        # Validate agent data structure
        if not isinstance(agent_data, dict):
            errors.append(f"Agent spec must be a dictionary/object, got: {type(agent_data).__name__}")
            return AgentValidation(
                is_valid=False,
                errors=errors,
                warnings=warnings,
                agent_name=agent_name
            )

        # Check required fields
        for field_name, field_desc in cls.REQUIRED_FIELDS.items():
            if field_name not in agent_data:
                errors.append(f"Missing required field '{field_name}': {field_desc}")
            elif not agent_data[field_name]:
                errors.append(f"Field '{field_name}' is empty: {field_desc}")

        # Validate component_type
        component_type = agent_data.get("component_type")
        if component_type and component_type not in cls.VALID_COMPONENT_TYPES:
            errors.append(
                f"Invalid component_type '{component_type}'. "
                f"Must be one of: {', '.join(cls.VALID_COMPONENT_TYPES)}"
            )

        # Extract agent metadata
        agent_name = agent_data.get("name", agent_dir.name)
        agent_description = agent_data.get("description")
        agentspec_version = agent_data.get("agentspec_version")

        # Check version compatibility
        if agentspec_version:
            if agentspec_version not in cls.SUPPORTED_VERSIONS:
                warnings.append(
                    f"Agent Spec version '{agentspec_version}' may not be fully supported. "
                    f"Tested versions: {', '.join(cls.SUPPORTED_VERSIONS)}"
                )

        # Check recommended fields
        for field_name, field_desc in cls.RECOMMENDED_FIELDS.items():
            if field_name not in agent_data:
                warnings.append(f"Missing recommended field '{field_name}': {field_desc}")

        # Validate system_prompt
        system_prompt = agent_data.get("system_prompt", "")
        if system_prompt:
            if len(system_prompt) < 100:
                warnings.append("System prompt is very short (< 100 chars). Consider adding more detail.")
            # Check for mission/focus content
            prompt_lower = system_prompt.lower()
            if not any(word in prompt_lower for word in ["mission", "goal", "purpose", "review", "audit"]):
                warnings.append("System prompt may be missing a clear mission statement")

        # Validate inputs schema
        inputs = agent_data.get("inputs", [])
        if inputs:
            input_errors = cls._validate_properties(inputs, "input")
            errors.extend(input_errors)

        # Validate outputs schema
        outputs = agent_data.get("outputs", [])
        if outputs:
            output_errors = cls._validate_properties(outputs, "output")
            errors.extend(output_errors)

        # Validate llm_config if present
        llm_config = agent_data.get("llm_config")
        if llm_config:
            llm_errors = cls._validate_llm_config(llm_config)
            errors.extend(llm_errors)

        # Validate tools if present
        tools = agent_data.get("tools", [])
        if tools:
            for i, tool in enumerate(tools):
                tool_errors = cls._validate_tool(tool, i)
                errors.extend(tool_errors)

        # Extract focus areas from metadata
        metadata = agent_data.get("metadata", {})
        if isinstance(metadata, dict):
            focus_areas = metadata.get("focus_areas", [])
            if not isinstance(focus_areas, list):
                focus_areas = []

        is_valid = len(errors) == 0

        return AgentValidation(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            agent_name=agent_name,
            agent_description=agent_description,
            focus_areas=focus_areas,
            agentspec_version=agentspec_version
        )

    @classmethod
    def _validate_properties(cls, properties: List[Any], prop_type: str) -> List[str]:
        """Validate input/output property definitions."""
        errors = []

        if not isinstance(properties, list):
            errors.append(f"{prop_type}s must be a list")
            return errors

        for i, prop in enumerate(properties):
            if not isinstance(prop, dict):
                errors.append(f"{prop_type}[{i}] must be an object")
                continue

            # Check required property fields
            if "title" not in prop:
                errors.append(f"{prop_type}[{i}] missing required field 'title'")

            if "type" not in prop:
                errors.append(f"{prop_type}[{i}] missing required field 'type'")
            elif prop["type"] not in cls.VALID_PROPERTY_TYPES:
                errors.append(
                    f"{prop_type}[{i}] has invalid type '{prop['type']}'. "
                    f"Must be one of: {', '.join(cls.VALID_PROPERTY_TYPES)}"
                )

        return errors

    @classmethod
    def _validate_llm_config(cls, llm_config: Dict[str, Any]) -> List[str]:
        """Validate LLM configuration."""
        errors = []

        if not isinstance(llm_config, dict):
            errors.append("llm_config must be an object")
            return errors

        # Check for component_type
        component_type = llm_config.get("component_type")
        valid_llm_types = {
            "VllmConfig", "OllamaConfig", "OpenAiCompatibleConfig",
            "OCIGenAIConfig", "LlmConfig"
        }

        if component_type and component_type not in valid_llm_types:
            errors.append(
                f"llm_config has unknown component_type '{component_type}'. "
                f"Known types: {', '.join(valid_llm_types)}"
            )

        # Check for essential LLM config fields
        if not llm_config.get("name") and not llm_config.get("model_id"):
            errors.append("llm_config should have 'name' or 'model_id'")

        return errors

    @classmethod
    def _validate_tool(cls, tool: Dict[str, Any], index: int) -> List[str]:
        """Validate a tool definition."""
        errors = []

        if not isinstance(tool, dict):
            errors.append(f"tools[{index}] must be an object")
            return errors

        # Tools need at least a name and description
        if "name" not in tool:
            errors.append(f"tools[{index}] missing required field 'name'")

        if "description" not in tool:
            errors.append(f"tools[{index}] missing required field 'description'")

        return errors

    @classmethod
    def validate_and_report(cls, agent_dir: Path) -> Tuple[bool, str]:
        """
        Validate agent and return simple pass/fail with message.

        Args:
            agent_dir: Path to agent directory

        Returns:
            Tuple of (is_valid, message)
        """
        validation = cls.validate_agent(agent_dir)
        return validation.is_valid, validation.get_report()


# Backward compatibility aliases
PersonaValidation = AgentValidation
PersonaValidator = AgentSpecValidator


def load_agent_spec(agent_dir: Path) -> Optional[Dict[str, Any]]:
    """
    Load and return the agent spec from a directory.

    Args:
        agent_dir: Path to agent directory

    Returns:
        Agent spec dictionary or None if not found/invalid
    """
    agent_yaml = agent_dir / "agent.yaml"
    agent_json = agent_dir / "agent.json"

    if agent_yaml.exists() and YAML_AVAILABLE:
        try:
            with open(agent_yaml, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load agent.yaml: {e}")
            return None

    if agent_json.exists():
        try:
            with open(agent_json, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent.json: {e}")
            return None

    # Fall back to legacy format
    legacy_file = agent_dir / "AI_START_HERE.md"
    if legacy_file.exists():
        logger.warning(f"Using legacy persona format from {legacy_file}")
        try:
            content = legacy_file.read_text(encoding='utf-8')
            # Convert legacy format to basic agent spec
            return {
                "component_type": "Agent",
                "name": agent_dir.name,
                "description": f"Legacy persona from {agent_dir.name}",
                "system_prompt": content,
                "inputs": [],
                "outputs": [],
                "tools": [],
            }
        except Exception as e:
            logger.error(f"Failed to load legacy persona: {e}")
            return None

    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python persona_validator.py <agent_dir>")
        print("\nValidates an agent directory against the Oracle Agent Spec.")
        print("See: https://oracle.github.io/agent-spec/26.1.0/")
        sys.exit(1)

    agent_dir = Path(sys.argv[1])
    validation = AgentSpecValidator.validate_agent(agent_dir)

    print(validation.get_report())
    sys.exit(0 if validation.is_valid else 1)
