"""Docker Compose generator — turns a UML DeploymentModel into docker-compose.yml."""
import inspect
import os
import re

from jinja2 import Environment, FileSystemLoader

from besser.BUML.metamodel.uml_deployment import (
    Artifact,
    CommunicationPath,
    DeploymentDependency,
    DeploymentModel,
    DeploymentRelation,
    Locality,
)
from besser.generators import GeneratorInterface
from besser.generators.agents.baf_generator import BAFGenerator
from besser.utilities import sort_by_timestamp


def _safe_service_name(name: str) -> str:
    """Convert a deployment element label to a Docker Compose-safe service/network name.

    Applies camelCase → snake_case conversion first, then lowercases everything
    and replaces runs of non-alphanumeric characters with a single underscore.

    Examples: "AgentRuntime" → "agent_runtime", "llm_gateway" → "llm_gateway",
              "LLMEndpoint" → "llm_endpoint", "Code Tester" → "code_tester".
    """
    s = (name or "").strip()
    # Insert underscore between a lowercase/digit and an uppercase (e.g. tR → t_R)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)
    # Insert underscore between a run of uppercase and the start of a new word
    # e.g. "LLMEndpoint" → "LLM_Endpoint"
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s or 'unnamed'


# v2 A2A — default system prompt when the Agent diagram carries no LLM-reply prompt.
def _default_prompt(name: str, role: str) -> str:
    if role == 'entry':
        return (f"You are {name}, the human-facing coordinator of an agent swarm. "
                f"Delegate the task to your team, then synthesize their results into "
                f"one concise answer for the user.")
    return (f"You are {name}, an agent in a collaborative swarm. Complete the task "
            f"you are given concisely; if you received collaborator inputs, use them.")


def _a2a_descriptor(agent, service_names: set, self_service: str = None) -> dict:
    """Classify a baked agent for A2A wiring from its boundary states (plan §3/§4).

    - `to_<peer>` / `from_<peer>` states name a peer; `_safe_service_name(suffix)`
      is matched against the swarm's service names. Peers NOT in `service_names`
      (e.g. a non-agentic `from_<Human>` handoff) are ignored — that's how the
      entry stays the entry despite an inbound boundary (R3, DAG-exact).
    - role = 'worker' iff it has at least one inbound peer that IS a service;
      else 'entry' (human-facing).
    - prompt = the first LLMReply prompt found on a non-boundary state, else a default.
    """
    # item 22 — this agent's own service id, so a self-referential `from_<self>`
    # boundary (the WME lane→Agent derivation mislabels the lane's own start/handoff
    # with the lane's own name) does NOT count as an inbound peer. `self_service` is
    # the artifact's safe service name (passed by the bake loop); fall back to the
    # agent name's safe form when called standalone (e.g. a unit test).
    self_id = self_service or _safe_service_name(getattr(agent, 'name', 'Agent'))
    to_peers, from_peers, prompt = [], [], None
    for st in getattr(agent, 'states', []) or []:
        nm = (getattr(st, 'name', '') or '')
        if nm.startswith('to_'):
            peer = _safe_service_name(nm[3:])
            if peer in service_names and peer != self_id:
                to_peers.append(peer)
        elif nm.startswith('from_'):
            peer = _safe_service_name(nm[5:])
            # Worker iff a `from_<peer>` resolves to a DIFFERENT swarm service.
            # `peer == self_id` is the self-referential `from_<self>` (item 22) and
            # must be ignored — otherwise the entry is wrongly demoted to a worker.
            if peer in service_names and peer != self_id:
                from_peers.append(peer)
        else:
            body = getattr(st, 'body', None)
            actions = getattr(body, 'actions', None) if body else None
            if actions and actions[0].__class__.__name__ == 'LLMReply':
                prompt = getattr(actions[0], 'prompt', None) or prompt
    role = 'worker' if from_peers else 'entry'
    name = getattr(agent, 'name', 'Agent')
    sorted_peers = sorted(set(to_peers))
    return {
        'role': role,
        'agent_id': _safe_service_name(name),
        'to_peers': sorted_peers,
        # item 10 — `peers` shim so the kind-aware template is single-path. The legacy
        # convention has no `kind`, so every peer renders as a plain channel (kind=None),
        # i.e. exactly today's broadcast fan-out behavior.
        'peers': [{'service': p, 'kind': None, 'order': i, 'state': ''}
                  for i, p in enumerate(sorted_peers)],
        'source': 'convention',
        'prompt': prompt or _default_prompt(name, role),
        'greeting': f"Hi! I'm {name}. Give me a task for the team.",
    }


def _resolve_peer_service(edge: dict, service_names: set):
    """Map an a2a edge's (ref|peer) to a swarm service name, else None.

    `ref` (the peer's AgentDiagram UUID) is the authoritative link, but the bake loop
    keys services by `_safe_service_name(artifact.name)`, not by UUID — so unless a
    {uuid → svc} index is threaded in (OQ-2) we address by `peer` name, matching how
    `_a2a_descriptor` already resolves to_/from_ peers. `ref` is recorded on the edge
    for traceability and future UUID-keyed addressing.
    """
    peer = _safe_service_name(edge.get("peer", ""))
    return peer if peer in service_names else None


def _a2a_descriptor_from_tags(agent, service_names: set, self_service: str = None) -> dict:
    """Build an A2A descriptor from agent._a2a (WME tags) — the preferred path (D3).

    Mirrors `_a2a_descriptor`'s contract (role/agent_id/to_peers/prompt/greeting) and
    adds `peers` (ordered, with kind) and `inbound` for the per-kind template. Peers not
    in `service_names` (dangling ref/name) are dropped — same tolerance as the legacy
    `from_<Human>` handoff.
    """
    tags = getattr(agent, '_a2a', None) or {}
    self_id = self_service or _safe_service_name(getattr(agent, 'name', 'Agent'))
    name = getattr(agent, 'name', 'Agent')

    peers, seen = [], set()                       # ordered, deduped, self-filtered
    for edge in tags.get('outbound', []):         # already order-sorted by the parser
        svc = _resolve_peer_service(edge, service_names)
        if svc and svc != self_id and svc not in seen:
            seen.add(svc)
            peers.append({'service': svc, 'kind': edge.get('kind'),
                          'order': edge.get('order', 9999),
                          'state': edge.get('state', '')})

    inbound_peers = {
        _resolve_peer_service(e, service_names)
        for e in tags.get('inbound', [])
    } - {None, self_id}
    role = 'worker' if inbound_peers else 'entry'

    # Prompt: reuse the first LLMReply prompt on a non-boundary state, else a default
    # (identical heuristic to _a2a_descriptor for parity).
    prompt = None
    for st in getattr(agent, 'states', []) or []:
        body = getattr(st, 'body', None)
        actions = getattr(body, 'actions', None) if body else None
        if actions and actions[0].__class__.__name__ == 'LLMReply':
            prompt = getattr(actions[0], 'prompt', None) or prompt
    return {
        'role': role,
        'agent_id': _safe_service_name(name),
        'to_peers': [p['service'] for p in peers],     # back-compat (existing template/tests)
        'peers': peers,                                # NEW: per-kind, ordered
        'inbound': sorted(inbound_peers),              # NEW
        'source': 'tags',                              # provenance (vs 'convention')
        'prompt': prompt or _default_prompt(name, role),
        'greeting': f"Hi! I'm {name}. Give me a task for the team.",
    }


class DockerComposeGenerator(GeneratorInterface):
    """Generate a docker-compose.yml from a UML DeploymentModel.

    Maps UML Deployment elements to Compose services and networks following
    the §3 table in the 05-docker-compose-generator-guide:

    - Artifact → service (locality decides ``build:`` vs ``image:``)
    - DeploymentRelation.multiplicity.max → ``deploy.replicas`` (when > 1)
    - Node → named network under ``networks:``
    - CommunicationPath → bridging ``<a>_<b>_link`` network both nodes join
    - DeploymentDependency (Artifact → Artifact) → ``depends_on:``
    - Interface / InterfaceProvided / InterfaceRequired → not mapped (v1)

    ``Artifact.manifests`` is emitted as a ``# manifests:`` comment only (v1
    — not resolved against the Component model; see guide §9 Q3).
    """

    def __init__(self, model: DeploymentModel, output_dir: str = None,
                 agent_models_by_id: dict = None):
        super().__init__(model, output_dir)
        # 6b-2 — {AgentDiagram-uuid → BUML Agent model}, supplied by the
        # project-level router handler. Empty on the single-diagram path, in
        # which case no build contexts are baked (6a compose-only behavior).
        self.agent_models_by_id = agent_models_by_id or {}

    def generate(self):
        file_path = self.build_generation_path(file_name="docker-compose.yml")
        templates_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "templates"
        )
        env = Environment(
            loader=FileSystemLoader(templates_path),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        entry_services = self._compute_entry_services()
        services, networks = self._build_view(self.model, entry_services)
        template = env.get_template("docker-compose.yml.j2")
        with open(file_path, mode="w", encoding="utf-8") as f:
            f.write(template.render(services=services, networks=networks))
        print("Code generated in the location: " + file_path)
        # 6b-2 — bake a BAF build context per resolvable agentic LOCAL artifact.
        self._bake_agent_contexts(env)

    def _bake_agent_contexts(self, env: Environment) -> None:
        """For each LOCAL Artifact carrying a resolvable ``agent_model_ref``,
        bake a build context (``<output_dir>/<svc_name>/``) containing the BAF
        ``agent.py`` + ``config.yaml`` (via BAFGenerator) and a ``Dockerfile``.

        The directory name is ``_safe_service_name(art.name)`` — identical to the
        ``build: ./<svc_name>`` the compose emits for this artifact, so the two
        line up. No-ops when ``agent_models_by_id`` is empty (single-diagram
        path). LOCAL artifacts with no/unresolvable ref are skipped (logged).
        """
        if not self.agent_models_by_id:
            return
        base_dir = os.path.dirname(
            self.build_generation_path(file_name="docker-compose.yml")
        )
        dockerfile_tpl = env.get_template("Dockerfile.j2")

        # v2 A2A — the set of swarm service names, so peer boundary states can be
        # resolved to real services (and non-service handoffs like `from_<Human>`
        # ignored). Mirrors the LOCAL+resolvable filter used in the bake loop.
        service_names = {
            _safe_service_name(a.name)
            for a in self.model.all_artifacts()
            if a.locality == Locality.LOCAL
            and getattr(a, "agent_model_ref", None)
            and self.agent_models_by_id.get(a.agent_model_ref) is not None
        }
        # The A2A agent template lives next to the generic BAF template
        # (agents/templates), not in this generator's templates dir, so it needs
        # its own loader rather than the docker_compose `env` above.
        agent_tpl_dir = os.path.join(
            os.path.dirname(inspect.getfile(BAFGenerator)), "templates"
        )
        a2a_env = Environment(
            loader=FileSystemLoader(agent_tpl_dir),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        a2a_tpl = a2a_env.get_template("baf_a2a_agent_template.py.j2")

        for art in self.model.all_artifacts():
            if art.locality != Locality.LOCAL:
                continue
            ref = getattr(art, "agent_model_ref", None)
            if not ref:
                continue
            agent = self.agent_models_by_id.get(ref)
            if agent is None:
                print(f"[docker_compose] artifact '{art.name}' references agent "
                      f"'{ref}' but no matching AgentDiagram was found — "
                      f"skipping its build context.")
                continue
            svc_name = _safe_service_name(art.name)
            ctx_dir = os.path.join(base_dir, svc_name)
            os.makedirs(ctx_dir, exist_ok=True)
            # BAF agent.py + config.yaml into the build context.
            BAFGenerator(agent, output_dir=ctx_dir).generate()

            # v2 A2A — if this agent has boundary states (it participates in the
            # swarm topology), OVERWRITE the generic agent.py with the A2A render.
            # item 22 — pass THIS service's name so a `from_<self>` boundary can't
            # demote the entry to a worker (the agent_id and the service line up via
            # _safe_service_name, but pass it explicitly to be robust).
            # item 10 — precedence: explicit WME a2a: tags (agent._a2a) ▸ the legacy
            # to_/from_ state-name convention ▸ self-contained. Absent tags ⇒ exactly the
            # current path (back-compat guarantee).
            if getattr(agent, '_a2a', None):
                descriptor = _a2a_descriptor_from_tags(agent, service_names, self_service=svc_name)
            else:
                descriptor = _a2a_descriptor(agent, service_names, self_service=svc_name)
            has_boundaries = bool(descriptor['to_peers']) or descriptor['role'] == 'worker'
            if has_boundaries:
                with open(os.path.join(ctx_dir, f"{agent.name}.py"),
                          mode="w", encoding="utf-8") as f:
                    f.write(a2a_tpl.render(agent=agent, a2a=descriptor))
                print(f"[docker_compose] A2A-wired ({descriptor['role']}): {svc_name} "
                      f"-> to_peers={descriptor['to_peers']}")

            # Dockerfile referencing the agent script (name unchanged).
            agent_script = f"{agent.name}.py"
            with open(os.path.join(ctx_dir, "Dockerfile"),
                      mode="w", encoding="utf-8") as f:
                f.write(dockerfile_tpl.render(agent_script=agent_script))
            print(f"[docker_compose] baked build context: {ctx_dir}")

    def _compute_entry_services(self) -> set:
        """Return the set of service names whose agent has role='entry'.

        Mirrors the LOCAL+resolvable filter in _bake_agent_contexts(). When
        agent_models_by_id is empty (single-diagram path) returns an empty set.
        """
        if not self.agent_models_by_id:
            return set()
        service_names = {
            _safe_service_name(a.name)
            for a in self.model.all_artifacts()
            if a.locality == Locality.LOCAL
            and getattr(a, 'agent_model_ref', None)
            and self.agent_models_by_id.get(a.agent_model_ref) is not None
        }
        result: set = set()
        for art in self.model.all_artifacts():
            if art.locality != Locality.LOCAL:
                continue
            ref = getattr(art, 'agent_model_ref', None)
            if not ref:
                continue
            agent = self.agent_models_by_id.get(ref)
            if agent is None:
                continue
            svc = _safe_service_name(art.name)
            # item 10 — same tag ▸ convention precedence as the bake loop, so the
            # entry/worker split (and the published ports) agree with what's baked.
            if getattr(agent, '_a2a', None):
                descriptor = _a2a_descriptor_from_tags(agent, service_names, self_service=svc)
            else:
                descriptor = _a2a_descriptor(agent, service_names, self_service=svc)
            if descriptor['role'] == 'entry':
                result.add(svc)
        return result

    def _build_view(self, model: DeploymentModel,
                    entry_services: set = None) -> tuple:
        """Resolve the metamodel into ordered dicts the template renders.

        Doing the graph walk here keeps the template declarative and lets tests
        assert on the view dicts directly.  Returns ``(services, networks)`` —
        plain-dict lists, deterministic order via ``sort_by_timestamp``.
        """
        all_artifacts = list(sort_by_timestamp(model.all_artifacts()))
        all_nodes = list(sort_by_timestamp(model.all_nodes()))
        all_rels = list(sort_by_timestamp(model.relationships))

        node_to_net = {id(n): _safe_service_name(n.name) for n in all_nodes}

        # ----- Pass 1: artifact → set of node python-ids it's deployed on ---
        # Sources: explicit DeploymentRelations AND containment (parent).
        art_nodes_map: dict = {id(a): set() for a in all_artifacts}

        for rel in all_rels:
            if isinstance(rel, DeploymentRelation):
                src_key = id(rel.source)
                if src_key in art_nodes_map:
                    art_nodes_map[src_key].add(id(rel.target))

        for art in all_artifacts:
            if art.parent is not None:
                art_nodes_map[id(art)].add(id(art.parent))

        # ----- Pass 2: networks per artifact (from node membership) ----------
        art_nets: dict = {id(a): [] for a in all_artifacts}

        def _add_net(art_key: int, net: str) -> None:
            if net not in art_nets[art_key]:
                art_nets[art_key].append(net)

        for art in all_artifacts:
            for node_id in art_nodes_map[id(art)]:
                net = node_to_net.get(node_id)
                if net:
                    _add_net(id(art), net)

        # ----- Pass 3: replicas from DeploymentRelation.multiplicity ---------
        art_replicas: dict = {}
        for rel in all_rels:
            if isinstance(rel, DeploymentRelation):
                src_key = id(rel.source)
                if src_key not in art_replicas:
                    mult = rel.multiplicity
                    if mult.max > 1:
                        art_replicas[src_key] = mult.max

        # ----- Pass 4a: DeploymentDependency → depends_on -------------------
        # source depends_on target (source must start after target is running).
        art_depends: dict = {id(a): [] for a in all_artifacts}
        for rel in all_rels:
            if isinstance(rel, DeploymentDependency):
                if isinstance(rel.source, Artifact) and isinstance(rel.target, Artifact):
                    svc_target = _safe_service_name(rel.target.name)
                    deps = art_depends[id(rel.source)]
                    if svc_target not in deps:
                        deps.append(svc_target)

        # ----- Pass 5: CommunicationPath → bridging networks ----------------
        cp_nets: list = []
        seen_cp: set = set()
        for rel in all_rels:
            if isinstance(rel, CommunicationPath):
                src_id = id(rel.source)
                tgt_id = id(rel.target)
                src_net = node_to_net.get(src_id, _safe_service_name(rel.source.name))
                tgt_net = node_to_net.get(tgt_id, _safe_service_name(rel.target.name))
                link = f"{src_net}_{tgt_net}_link"
                if link not in seen_cp:
                    seen_cp.add(link)
                    cp_nets.append(link)
                for art in all_artifacts:
                    if (src_id in art_nodes_map[id(art)]
                            or tgt_id in art_nodes_map[id(art)]):
                        _add_net(id(art), link)

        # ----- Build service dicts -------------------------------------------
        # Capability/resource stereotype tokens — these artifacts represent
        # externally hosted services (LLM API, vector DB, RAG store), not
        # deployable containers.
        _CAP_TOKENS = frozenset(['llm', 'db', 'rag', 'tool', 'skill'])
        _entry = entry_services or set()

        services = []
        for art in all_artifacts:
            # D12 synthetic artifacts (created from WME DeploymentComponent)
            # have art.manifests != [] — they are logical component projections
            # that duplicate the physical artifact's service name.
            if art.manifests:
                continue

            # Capability/resource artifacts are hosted externally, not built
            # as Docker images.
            if any(t in _CAP_TOKENS for t in art.stereotypes):
                continue

            svc_name = _safe_service_name(art.name)
            art_key = id(art)

            if art.locality == Locality.LOCAL:
                build = f"./{svc_name}"
                image = None
                is_hybrid = False
            elif art.locality == Locality.EXTERNAL:
                build = None
                image = f"{svc_name}:latest"
                is_hybrid = False
            else:  # HYBRID
                build = None
                image = f"{svc_name}:latest"
                is_hybrid = True

            services.append({
                'name': svc_name,
                'build': build,
                'image': image,
                'is_hybrid': is_hybrid,
                'networks': art_nets.get(art_key, []),
                'replicas': art_replicas.get(art_key),
                'depends_on': art_depends.get(art_key, []),
                'ports': ['5001:5000', '8765:8765'] if svc_name in _entry else [],
                'stereotypes': ', '.join(art.stereotypes) if art.stereotypes else None,
                'manifests': ', '.join(art.manifests) if art.manifests else None,
            })

        # ----- Build network dicts ------------------------------------------
        seen_nets: set = set()
        networks = []
        for node in all_nodes:
            net_name = node_to_net[id(node)]
            if net_name not in seen_nets:
                seen_nets.add(net_name)
                kind_comment = f"  # kind: {node.kind.value}" if node.kind else ""
                networks.append({'name': net_name, 'kind_comment': kind_comment})
        for link in cp_nets:
            if link not in seen_nets:
                seen_nets.add(link)
                networks.append({'name': link, 'kind_comment': ""})

        return services, networks
