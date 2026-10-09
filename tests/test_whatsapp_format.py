from orq.core.whatsapp_format import split_message, to_whatsapp


def test_markdown_becomes_whatsapp_formatting() -> None:
    md = ("## Módulos\n\n**slug.py** faz __isso__ e veja [a doc](https://x.dev).\n\n"
          "| Módulo | Conteúdo |\n|---|---|\n| `greet.py` | saudações |\n| `slug.py` | slugify |\n\n```python\nx = 1\n```")

    assert to_whatsapp(md) == ("*Módulos*\n\n*slug.py* faz _isso_ e veja a doc (https://x.dev).\n\n"
                               "• `greet.py` · saudações\n• `slug.py` · slugify\n\n```python\nx = 1\n```")


def test_code_blocks_are_left_alone() -> None:
    assert to_whatsapp("```\n## not a heading\n**x**\n```") == "```\n## not a heading\n**x**\n```"


def test_short_messages_are_not_split() -> None:
    assert split_message("curta", limit=100) == ["curta"]


def test_long_messages_split_on_paragraphs_then_lines_with_markers() -> None:
    paragraphs = [f"parágrafo {i} " + "x" * 40 for i in range(6)]
    parts = split_message("\n\n".join(paragraphs), limit=120)

    assert len(parts) > 1 and all(len(p) <= 120 for p in parts)
    assert parts[0].startswith(f"(1/{len(parts)})\n") and parts[-1].startswith(f"({len(parts)}/{len(parts)})\n")
    assert "".join(p.split("\n", 1)[1] for p in parts).replace("\n", "") == "".join(paragraphs)


def test_a_single_huge_line_is_cut() -> None:
    parts = split_message("y" * 250, limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(p.split("\n", 1)[1] for p in parts) == "y" * 250
