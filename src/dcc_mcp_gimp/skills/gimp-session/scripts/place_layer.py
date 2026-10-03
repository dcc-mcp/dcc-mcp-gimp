from dcc_mcp_core.skill import run_main

from dcc_mcp_gimp.skill_tools import bridge_main

main = bridge_main(
    "gimp.place_layer", "Place a layer into a group and set its native pixel offsets."
)

if __name__ == "__main__":
    run_main(main)
