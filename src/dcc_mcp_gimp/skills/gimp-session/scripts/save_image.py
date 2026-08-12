from dcc_mcp_core.skill import run_main

from dcc_mcp_gimp.skill_tools import bridge_main

main = bridge_main("gimp.save_image", "GIMP image saved.")

if __name__ == "__main__":
    run_main(main)
