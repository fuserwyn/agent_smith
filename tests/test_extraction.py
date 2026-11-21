from agent_smith.extraction import extract_code


def test_python_block_primary():
    e = extract_code("Thought: hi\n```python\nprint(1)\n```<end_code>")
    assert e.code == "print(1)" and e.format == "python_block" and not e.notes


def test_unclosed_block_is_interpreted_with_note():
    e = extract_code("```python\nx = 1\nprint(x)\n")
    assert e.code == "x = 1\nprint(x)" and "not closed" in e.notes[0]


def test_multiple_blocks_joined():
    e = extract_code("```python\na = 1\n```\ntext\n```py\nprint(a)\n```")
    assert "a = 1" in e.code and "print(a)" in e.code and "sequentially" in e.notes[0]


def test_xml_invoke_converted():
    e = extract_code('<invoke name="read_file"><parameter name="filepath">/testbed/x.py</parameter>'
                     '<parameter name="end_line">20</parameter></invoke>')
    assert e.code == "result = read_file(filepath='/testbed/x.py', end_line=20)\nprint(result)"


def test_hermes_tool_call_converted_even_when_stop_sequence_removed_closing_tag():
    e = extract_code('<tool_call>{"name": "search_code", "arguments": {"pattern": "def f"}}')
    assert e.code == "result = search_code(pattern='def f')\nprint(result)"


def test_react_final_answer():
    e = extract_code('Thought: ok\nAction: final_answer\nAction Input: {"answer": "42"}')
    assert e.code == "final_answer('42')"


def test_repl_prompts_and_indentation_repaired():
    assert extract_code("```python\n>>> x = 2\n>>> print(x)\n```").code == "x = 2\nprint(x)"
    e = extract_code("```python\n    y = 1\n    print(y)\n```")
    assert e.code == "y = 1\nprint(y)" and "dedented" in e.notes[0]


def test_no_code():
    assert not extract_code("The answer is 42.").found


def test_a_block_whose_fence_was_taken_by_the_stop_sequence_is_whole():
    """Providers cut at <end_code> and often drop the ``` before it: with a normal finish that is not an error."""
    e = extract_code('Thought: list.\n```python\nrun_command("ls")', complete=True)
    assert e.code == 'run_command("ls")' and e.format == "python_block" and not e.notes
    cut = extract_code('```python\nx = (1,\n', complete=False)               # cut by the output limit: still reported
    assert cut.format == "python_block_unclosed" and cut.notes


def test_markdown_fences_inside_python_strings_do_not_end_the_block():
    for text in ('```python\nfinal_answer("""## Done\n\n```swift\nlet a = 1\n```\n""")\n```<end_code>',
                 '```python\nwrite_file("README.md", """# App\n```bash\nmake\n```\n""")'):
        e = extract_code(text, complete=True)
        compile(e.code, "<step>", "exec")                                       # the whole program, not cut at ```
        assert "```" in e.code and not e.notes
