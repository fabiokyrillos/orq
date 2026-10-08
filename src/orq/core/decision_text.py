"""Context, consequences and recommendation reasons for the decisions orq raises itself (Phase 6).

Each builder returns the keyword arguments `Decision` takes for these fields. Pure functions over the evidence the
loop already has; the owner reads the result on a phone, so it says what happened, why it matters, and what each
answer will do. Phase 6.1: owner-facing text in Brazilian Portuguese (CLAUDE.md exception); `stakes` rates how much is
at stake (only "low" can ever be answered automatically).
"""

from __future__ import annotations

GUARD_RULES = {
    "recursive_delete": "apaga arquivos ou pastas recursivamente; um caminho errado perde trabalho",
    "git_reset_hard": "descarta mudanças não commitadas no worktree",
    "git_force_push": "reescreve o histórico da branch remota",
    "git_branch_delete": "apaga uma branch do git",
    "git_clean": "apaga arquivos não versionados do worktree",
    "dependency_removal": "remove uma dependência que o projeto ainda pode usar",
    "sql_destructive": "apaga ou esvazia objetos do banco de dados",
    "write_outside_worktree": "escreve fora do worktree da run, em outro lugar do seu PC",
    "protected_path": "altera um caminho que você marcou como protegido",
}
DIFF_RULES = {
    "deleted_file": "arquivo apagado",
    "removed_test": "teste removido",
    "removed_export": "função ou classe exportada sumiu",
    "removed_route": "rota HTTP sumiu",
    "protected_path": "caminho protegido alterado",
    "dependency_removed": "dependência removida de um manifesto",
    "negative_balance": "muito mais código apagado que adicionado",
}


def _milestone(milestone: dict | None) -> str:
    return f"No milestone \"{milestone['title']}\", " if milestone and milestone.get("title") else ""


def from_model(human: dict) -> dict:
    """The fields a reviewer, planner or implementer filled; tolerant of the shape before Phase 6."""
    details = human.get("option_details") or []
    stakes = str(human.get("stakes") or "").strip().lower()
    return {"context": str(human.get("context") or "").strip(),
            "option_details": [str(d) for d in details] if isinstance(details, list) else [],
            "recommendation_reason": str(human.get("recommendation_reason") or "").strip(),
            "stakes": stakes if stakes in ("low", "medium", "high") else ""}


def plan_approval(plan: dict) -> dict:
    lines = [f"{i + 1}. {m['title']} [{m['difficulty']}]: {m['goal']}\n   Pronto quando: {m['done_when']}"
             for i, m in enumerate(plan.get("milestones") or [])]
    summary = str(plan.get("summary") or "").strip()
    return {"context": (summary + "\n\n" if summary else "") + "\n".join(lines),
            "option_details": ["o implementador começa o milestone 1 na hora",
                               "o planejador planeja de novo; responda com texto dizendo o que mudar"],
            "recommendation_reason": "o plano fica dentro do escopo da task; aprove a não ser que falte ou sobre um milestone",
            "stakes": "high"}


def guard_pre(description: str, rule: str | None, milestone: dict | None) -> dict:
    why = GUARD_RULES.get(rule or "", "está na lista de ações destrutivas do guard")
    text = f"{_milestone(milestone)}o implementador tentou rodar `{description}`.\nO guard bloqueou porque {why}."
    return {"context": text[0].upper() + text[1:],
            "option_details": ["essa ação exata roda uma vez no próximo turno do implementador; qualquer outra continua bloqueada",
                               "o implementador é avisado para terminar o trabalho sem ela"],
            "recommendation_reason": "o orq nunca aprova ação destrutiva às cegas; aprove só se a task precisa exatamente disso",
            "stakes": "high"}


def guard_diff(violations: list, numstat: list[tuple[int, int, str]], report: str = "") -> dict:
    counts = {path: (added, deleted) for added, deleted, path in numstat}
    lines = []
    for v in violations:
        added, deleted = counts.get(v.path, (0, 0))
        lines.append(f"• {v.path}: {DIFF_RULES.get(v.rule, v.rule)} [{v.rule}] ({v.detail}; +{added}/-{deleted} linhas)")
    said = " ".join(report.split())[:400]
    return {"context": "As mudanças desta iteração dispararam o guard de diff:\n" + "\n".join(lines) +
                       (f"\n\nO implementador disse: {said}" if said else ""),
            "option_details": ["as mudanças ficam; o check roda e a iteração é commitada",
                               "as mudanças deste turno são descartadas (volta para a última iteração) e o implementador segue sem elas"],
            "recommendation_reason": "remoções que a task não pediu costumam ser erro; aprove se a task exige",
            "stakes": "high"}


def secret(findings: list[dict]) -> dict:
    lines = [f"• {f.get('RuleID')} em {f.get('File')}, linha {f.get('StartLine')}" for f in findings]
    return {"context": "O gitleaks achou o que parecem segredos nas mudanças (valores escondidos):\n" + "\n".join(lines),
            "option_details": ["depois que você remover o segredo no worktree, o orq verifica de novo e continua",
                               "a run para (ABORTED); nada é commitado nem enviado"],
            "recommendation_reason": "os repos são públicos; um segredo enviado precisa ser trocado, então nada é commitado até a verificação passar",
            "stakes": "high"}


def no_progress(rule: str, detail: str, target: str, summaries: list[str]) -> dict:
    recent = "\n".join(f"• {s}" for s in summaries[-3:] if s)
    return {"context": f"A run não está progredindo ({rule}: {detail})." + (f"\n\nÚltimas revisões:\n{recent}" if recent else ""),
            "option_details": ["segue como está; o contador de travamento recomeça",
                               f"{target}: o trabalho posterior é descartado e os dois agentes são avisados do que saiu",
                               "a run para (ABORTED)"],
            "recommendation_reason": "voltar ao último estado bom costuma destravar mais rápido que outra tentativa",
            "stakes": "medium"}


def agent_error(role: str, kind: str, error: str) -> dict:
    return {"context": f"A chamada do {role} falhou ({kind}).\nErro: {error.strip()[:400]}",
            "option_details": ["o mesmo passo roda de novo", "a run para (ABORTED)"],
            "recommendation_reason": "a maioria das falhas é passageira (rede, CLI reiniciando); vale tentar de novo uma vez",
            "stakes": "low"}


def ci_none(pr_number: int | None) -> dict:
    return {"context": f"O PR #{pr_number} não tem checks do GitHub depois do prazo de espera, então o CI não confirma a mudança.",
            "option_details": ["a revisão final roda e o PR é mergeado sem CI", "a run para (ABORTED); o PR continua aberto"],
            "recommendation_reason": "sem CI, só o check local sustenta o merge; aborte se o repo deveria ter CI",
            "stakes": "high"}


def ci_timeout(pr_number: int | None, checks: list[dict]) -> dict:
    states = ", ".join(f"{c.get('name')}: {c.get('bucket')}" for c in checks) or "nenhum informado"
    return {"context": f"Os checks do GitHub no PR #{pr_number} não terminaram no prazo ({states}).",
            "option_details": ["o orq espera o CI de novo", "a run para (ABORTED); o PR continua aberto"],
            "recommendation_reason": "runners gratuitos às vezes demoram; esperar mais uma vez custa pouco",
            "stakes": "low"}


def rebase_conflict(files: list[str], reason: str) -> dict:
    return {"context": f"A branch base andou e o rebase deu conflito em {', '.join(files) or 'alguns arquivos'}.\n"
                       f"O implementador tentou resolver e o orq desfez a tentativa: {reason.strip()[:400]}",
            "option_details": ["depois que você resolver o conflito à mão no worktree, o gate recomeça (rebase, CI, revisão)",
                               "a run para (ABORTED); o PR continua aberto"],
            "recommendation_reason": "um conflito que o implementador não resolve costuma exigir escolher entre duas mudanças; só você pode",
            "stakes": "high"}


def gate_rounds(rounds: int, limit: int, reason: str) -> dict:
    return {"context": f"O gate de merge falhou {rounds} vezes (limite {limit}).\nÚltimo motivo: {reason.strip()[:400]}",
            "option_details": ["o implementador ganha mais uma rodada com a última falha; o contador recomeça",
                               "a run para (ABORTED); o PR continua aberto"],
            "recommendation_reason": "falhas repetidas no gate costumam significar que a task precisa de você; aborte a não ser que o último motivo pareça simples",
            "stakes": "high"}
