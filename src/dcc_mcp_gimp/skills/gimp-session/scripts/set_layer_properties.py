from dcc_mcp_core.skill import run_main

from dcc_mcp_gimp.skill_tools import bridge_main

main = bridge_main("gimp.set_layer_properties", "GIMP layer properties updated.")

if __name__ == "__main__":
    run_main(main)
