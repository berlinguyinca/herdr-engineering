// The ids every fixture module shares, so one fictional fleet is told consistently on every page. Never random, never time-derived.
export const IDS = {
  hosts: ['beast', 'bender', 'zapp', 'nibbler'],
  sessions: ['s-101', 's-102', 's-103', 's-104'],          // s-101 is the one the detail pages open
  missions: ['M-301', 'M-302', 'M-303', 'M-304'],           // herdr-sourced, same as visual-dashboard.mjs; pi:M-201..204 are the live ones
  piMissions: ['pi:M-201', 'pi:M-202', 'pi:M-203', 'pi:M-204'],
  services: ['svc-1', 'svc-2', 'svc-3'],
  repo: 'org/repo', pr: 7, artifact: 'art-1', assistant: 'asst_1',
};
