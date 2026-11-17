import unittest
from pathlib import Path
from lakemirror.geofence_evidence import inspect_geofence


class GeofenceEvidenceTests(unittest.TestCase):
    def test_polar_arc_and_date_line_hole_are_real_fixture_evidence(self):
        root=Path(__file__).resolve().parents[1]
        result=inspect_geofence(root/'fixtures/geofence/contract.json')
        self.assertEqual([(s['id'],s['longitude'],s['latitude']) for s in result['shipments']],[('S1',0.0,82.0),('S2',178.5,10.0),('S3',179.5,10.0)])
        self.assertEqual([z['zone_id'] for z in result['zones']],['polar-band','date-line-band'])
        self.assertEqual(len(result['zones'][1]['rings']),2)
        self.assertGreater(result['polar_geodesic_midpoint_witness']['top_lat'],84.9)
        self.assertLess(result['polar_geodesic_midpoint_witness']['bottom_lat'],74)
        self.assertIsNone(result['zone_memberships'])
