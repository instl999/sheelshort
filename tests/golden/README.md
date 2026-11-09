# Golden files

Regression fixtures: `test_golden_prompts` rebuilds every H3 prompt of the Yunjing showcase (`lan/`, a copy of
`projects/lan`'s bible and D01) and `test_golden_graphs` the render graphs of the production and tuned U22, and
both compare them with the files here. A deliberate change of the engine's wording or of a graph is accepted with

    GOLDEN_UPDATE=1 python tests/test_offline.py

and the diff of these files is reviewed with the code change.
