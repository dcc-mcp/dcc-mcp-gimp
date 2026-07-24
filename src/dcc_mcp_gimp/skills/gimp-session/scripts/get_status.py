from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_gimp.bridge import get_bridge


@skill_entry
def main(**_kwargs):
    return skill_success("GIMP bridge is ready.", **get_bridge().call("gimp.get_status"))


if __name__ == "__main__":
    run_main(main)
