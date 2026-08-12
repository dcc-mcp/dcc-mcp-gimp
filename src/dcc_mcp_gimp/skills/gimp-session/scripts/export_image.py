from dcc_mcp_core.skill import run_main

from dcc_mcp_gimp.skill_tools import bridge_main

main = bridge_main("gimp.export_image", "GIMP image exported.")

if __name__ == "__main__":
    run_main(main)
