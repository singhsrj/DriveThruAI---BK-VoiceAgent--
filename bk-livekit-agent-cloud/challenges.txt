Challenge: MCP menu tools not loading on LiveKit Cloud

Problem: The BK voice agent worked perfectly in local console mode, but after deploying to LiveKit Cloud it only had the place_order tool. The MCP menu tools (list_items, search_items, list_combos, etc.) never loaded, and no error surfaced, because LiveKit silently continues without tools when an MCP connection fails.

Debugging:

Tested the MCP server in isolation. A standalone MCP client listed all 10 tools, which ruled out both the server and the agent code.
Confirmed from the deployment logs that the new code was live and MCP_SERVER_URL was correct.
That narrowed it to the network layer: the laptop could reach the EC2 instance, but LiveKit Cloud (India South region) could not.

Root cause: The EC2 Security Group's inbound rule for port 8943 did not allow traffic from LiveKit Cloud's IP addresses.

Fix: Updated the inbound rule so TCP 8943 was reachable from the cloud. The agent then loaded the menu tools and worked end to end.

Other issues solved during the same deployment:

The cloud folder's agent.py was still the template version, so LiveKit Inference's LLM was running and threw a 402 credits error. Replacing it with the merged script fixed that.
A misconfigured .env.local.
A 401 Unauthorized on lk agent deploy, caused by the CLI's default project not matching the authenticated project.